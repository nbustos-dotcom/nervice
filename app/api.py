"""Nervice as a network API — a third face on the same brain (the local nervice.py / voice.py
loops are untouched and still direct-call). Reuses respond() + save_exchange(), so every existing
jail, scrub, gate, and timeout applies unchanged. Intended to be reached only over Nate's private
Tailscale tailnet (WireGuard-encrypted); bound to 127.0.0.1 for now. NEVER expose publicly.

Every endpoint requires:  Authorization: Bearer <NERVICE_API_TOKEN>
"""
import io
import os
import sys
import wave
import uuid
import time
import base64
import secrets
import asyncio
import tempfile
import importlib
import threading
from contextlib import asynccontextmanager

import pathlib

import numpy as np
from fastapi import (FastAPI, Depends, Header, HTTPException, UploadFile, File, Form,
                     WebSocket, WebSocketDisconnect)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from groq import RateLimitError
from app.chat import respond, save_exchange
from app.llm import rate_limit_message
from app.streaming import stream_reply

USER = "nate"
_TOKEN = os.environ.get("NERVICE_API_TOKEN")
WINDOW_MAX = 12

# Per-conversation sliding window of the last WINDOW_MAX messages, in-process (single user).
# CAVEAT: lost on restart — but that's only the verbatim recent-turns buffer; durable facts about
# Nate live in the memory DB (retrieved fresh each turn), so a restart loses chat scrollback, not
# what Nervice knows about him.
_windows: dict[str, list] = {}
_pending: set = set()  # fire-and-forget save tasks (same pattern as the local loops)

# Voice-model warm state: cold -> warming -> warm (or failed: ...). Warmed in a background
# thread at startup so the first phone /voice call doesn't eat the 7-12s model load.
_voice_state = {"status": "cold"}


def _warm_voice():
    try:
        print("[api] voice models warming...", file=sys.stderr)
        t0 = time.time()
        importlib.import_module("app.voice")   # import lock makes concurrent /voice waits safe
        _voice_state["status"] = "warm"
        print(f"[api] voice models warm ({time.time() - t0:.1f}s)", file=sys.stderr)
    except Exception as e:
        _voice_state["status"] = f"failed: {repr(e)[:80]}"
        print(f"[api] voice warm FAILED: {repr(e)[:120]}", file=sys.stderr)


async def _db_keepalive(stop: asyncio.Event, interval_s: float = 60.0):
    """Ping the pooled Supabase connection so the remote pooler never idles it out — a real turn
    then pays warm retrieval (~0.3-0.8s) instead of the ~2.1s reconnect measured after idle.
    Ping errors are logged and the loop keeps going (pre_ping covers the next real checkout).
    Shutdown is an Event, NOT task.cancel(): cancelling mid-checkout rips through SQLAlchemy's
    greenlet bridge and leaks the connection — with the event, a mid-flight ping always completes
    and checks its connection back in before the loop exits."""
    from sqlalchemy import text
    from app.db import engine
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            break                       # stop requested while sleeping
        except asyncio.TimeoutError:
            pass                        # interval elapsed — ping
        try:
            async with engine.connect() as c:
                await c.execute(text("SELECT 1"))
        except Exception as e:
            print(f"[api] db keepalive failed: {repr(e)[:80]}", file=sys.stderr)


@asynccontextmanager
async def lifespan(_app):
    _voice_state["status"] = "warming"
    threading.Thread(target=_warm_voice, daemon=True).start()
    stop = asyncio.Event()
    keepalive = asyncio.create_task(_db_keepalive(stop))
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(keepalive, timeout=10)   # let a mid-flight ping finish
        except (asyncio.TimeoutError, asyncio.CancelledError):
            keepalive.cancel()                              # hung network — last resort



app = FastAPI(title="Nervice API", lifespan=lifespan)

# The phone client (static files) is served UNauthenticated — acceptable ONLY because the page is
# inert without a token and is reachable only over the private tailnet. Every API route below keeps
# its Bearer auth; the static mount does not bypass it.
_STATIC = pathlib.Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(_STATIC / "index.html"))


async def auth(authorization: str = Header(None)):
    """Bearer-token gate on every route. Constant-time compare; reject missing/wrong with 401."""
    if not _TOKEN:
        raise HTTPException(status_code=500, detail="NERVICE_API_TOKEN not configured on the server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(authorization[7:], _TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


def _store(conversation_id: str, user_message: str, reply: str) -> None:
    """Fire-and-forget persistence so the response returns immediately; errors logged, never raised."""
    async def _run():
        try:
            await save_exchange(USER, conversation_id, user_message, reply)
        except Exception as e:
            print(f"[api store failed] {repr(e)[:120]}", file=sys.stderr)
    t = asyncio.create_task(_run())
    _pending.add(t)
    t.add_done_callback(_pending.discard)


def _advance_window(cid: str, user_message: str, reply: str) -> None:
    w = _windows.setdefault(cid, [])
    w.append({"role": "user", "content": user_message})
    w.append({"role": "assistant", "content": reply})
    _windows[cid] = w[-WINDOW_MAX:]


class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None


@app.get("/health", dependencies=[Depends(auth)])
async def health():
    # Voice models are warmed by the startup thread; report where that stands without forcing a load.
    voice = _voice_state["status"]
    if voice == "warm" and "app.voice" in sys.modules:
        v = sys.modules["app.voice"]
        return {"status": "ok", "voice": "warm", "stt": v.WHISPER_PATH, "tts": v.TTS_ENGINE}
    return {"status": "ok", "voice": voice, "stt": "faster-whisper base.en", "tts": "kokoro"}


@app.post("/chat", dependencies=[Depends(auth)])
async def chat(inp: ChatIn):
    cid = inp.conversation_id or str(uuid.uuid4())
    window = _windows.setdefault(cid, [])
    try:
        reply = await respond(USER, inp.message, window, voice_mode=False)
    except RateLimitError as e:   # backstop — respond() already handles 429, this guards any new path
        return {"reply": rate_limit_message(e), "conversation_id": cid}
    _advance_window(cid, inp.message, reply)
    _store(cid, inp.message, reply)
    return {"reply": reply, "conversation_id": cid}


@app.post("/voice", dependencies=[Depends(auth)])
async def voice(audio: UploadFile = File(...), conversation_id: str | None = Form(None)):
    # Models are normally warm (startup thread); if a call lands mid-warm, this import blocks on
    # the import lock until ready instead of failing. Text-only API use never needs them.
    t_total = time.time()
    import app.voice as v

    data = await audio.read()
    suffix = os.path.splitext(audio.filename or "")[1] or ".bin"
    tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    t0 = time.time()
    try:
        tf.write(data)
        tf.close()
        transcript = v.transcribe_file(tf.name)   # av decodes webm/opus/ogg/wav — no ffmpeg binary
    finally:
        os.unlink(tf.name)
    stt_s = time.time() - t0

    cid = conversation_id or str(uuid.uuid4())
    if not transcript.strip():
        print(f"[voice-timing] stt={stt_s:.2f}s llm=0 tts=0 total={time.time()-t_total:.2f}s (no speech)",
              file=sys.stderr)
        return {"transcript": "", "reply": "(no speech detected)", "conversation_id": cid, "audio_wav_base64": ""}

    window = _windows.setdefault(cid, [])
    t0 = time.time()
    try:
        reply = await respond(USER, transcript, window, voice_mode=True)
    except RateLimitError as e:   # backstop — reply still gets synthesized below so the phone speaks it
        reply = rate_limit_message(e)
    llm_s = time.time() - t0
    _advance_window(cid, transcript, reply)
    _store(cid, transcript, reply)

    # Synthesize the reply to a WAV and return it base64-encoded in the JSON. The phone client
    # decodes audio_wav_base64 -> bytes -> Blob({type:'audio/wav'}) and plays it.
    t0 = time.time()
    pcm, sr = v.synth_to_pcm(reply)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    audio_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    tts_s = time.time() - t0
    print(f"[voice-timing] stt={stt_s:.2f}s llm={llm_s:.2f}s tts={tts_s:.2f}s "
          f"total={time.time()-t_total:.2f}s", file=sys.stderr)
    return {"transcript": transcript, "reply": reply, "conversation_id": cid, "audio_wav_base64": audio_b64}


# --------------- WebSocket streaming (Phase 1) ---------------
# Browsers cannot set headers on WebSocket connects, so the Bearer token rides in the FIRST
# message frame — never the URL (URLs land in logs). Nothing is processed before the token
# verifies; bad/missing token closes with 1008 (policy violation). Same token, same boundary,
# same tailnet-only bind as the REST routes.

async def _ws_handshake(ws: WebSocket) -> dict | None:
    """Accept, read the first frame, verify the token. Returns the first frame on success;
    closes 1008 and returns None otherwise."""
    await ws.accept()
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close(code=1008)
        return None
    token = first.get("token") or ""
    if not _TOKEN or not isinstance(token, str) or not secrets.compare_digest(token, _TOKEN):
        await ws.close(code=1008)
        return None
    await ws.send_json({"type": "ready"})
    return first


def _ws_sender(ws: WebSocket):
    """Serialized frame sender — the audio pipeline task and the stream loop both send; the lock
    keeps frames whole and ordered on the wire."""
    lock = asyncio.Lock()
    async def send(frame: dict):
        async with lock:
            await ws.send_json(frame)
    return send


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    first = await _ws_handshake(ws)
    if first is None:
        return
    cid = first.get("conversation_id") or str(uuid.uuid4())
    want_audio = bool(first.get("audio", False))
    send = _ws_sender(ws)
    try:
        while True:
            msg = await ws.receive_json()
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            window = _windows.setdefault(cid, [])
            reply = await stream_reply(USER, text, window, send, voice=want_audio,
                                       conversation_id=cid)
            _advance_window(cid, text, reply)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    first = await _ws_handshake(ws)
    if first is None:
        return
    cid = first.get("conversation_id") or str(uuid.uuid4())
    send = _ws_sender(ws)
    import app.voice as v   # blocks on the warm thread's import lock if mid-warm, like REST
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data:
                continue   # ignore stray text frames between utterances
            tf = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
            try:
                tf.write(data)
                tf.close()
                transcript = await asyncio.to_thread(v.transcribe_file, tf.name)
            finally:
                os.unlink(tf.name)
            await send({"type": "transcript", "text": transcript})
            if not transcript.strip():
                await send({"type": "done", "reply": ""})
                continue
            window = _windows.setdefault(cid, [])
            reply = await stream_reply(USER, transcript, window, send, voice=True,
                                       conversation_id=cid)
            _advance_window(cid, transcript, reply)
    except WebSocketDisconnect:
        pass

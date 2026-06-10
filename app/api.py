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
import base64
import secrets
import asyncio
import tempfile

import numpy as np
from fastapi import FastAPI, Depends, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from app.chat import respond, save_exchange

USER = "nate"
_TOKEN = os.environ.get("NERVICE_API_TOKEN")
WINDOW_MAX = 12

# Per-conversation sliding window of the last WINDOW_MAX messages, in-process (single user).
# CAVEAT: lost on restart — but that's only the verbatim recent-turns buffer; durable facts about
# Nate live in the memory DB (retrieved fresh each turn), so a restart loses chat scrollback, not
# what Nervice knows about him.
_windows: dict[str, list] = {}
_pending: set = set()  # fire-and-forget save tasks (same pattern as the local loops)

app = FastAPI(title="Nervice API")


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
    # Lazy: do NOT import app.voice here (importing it loads whisper + Kokoro). Report load state.
    if "app.voice" in sys.modules:
        v = sys.modules["app.voice"]
        return {"status": "ok", "stt": v.WHISPER_PATH, "tts": v.TTS_ENGINE}
    return {"status": "ok", "stt": "faster-whisper base.en (lazy)", "tts": "kokoro (lazy — loads on first /voice)"}


@app.post("/chat", dependencies=[Depends(auth)])
async def chat(inp: ChatIn):
    cid = inp.conversation_id or str(uuid.uuid4())
    window = _windows.setdefault(cid, [])
    reply = await respond(USER, inp.message, window, voice_mode=False)
    _advance_window(cid, inp.message, reply)
    _store(cid, inp.message, reply)
    return {"reply": reply, "conversation_id": cid}


@app.post("/voice", dependencies=[Depends(auth)])
async def voice(audio: UploadFile = File(...), conversation_id: str | None = Form(None)):
    # Lazy model load: first /voice call pulls in app.voice (whisper + Kokoro). Text-only API
    # use never loads the voice models.
    import app.voice as v

    data = await audio.read()
    suffix = os.path.splitext(audio.filename or "")[1] or ".bin"
    tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tf.write(data)
        tf.close()
        transcript = v.transcribe_file(tf.name)   # av decodes webm/opus/ogg/wav — no ffmpeg binary
    finally:
        os.unlink(tf.name)

    cid = conversation_id or str(uuid.uuid4())
    if not transcript.strip():
        return {"transcript": "", "reply": "(no speech detected)", "conversation_id": cid, "audio_wav_base64": ""}

    window = _windows.setdefault(cid, [])
    reply = await respond(USER, transcript, window, voice_mode=True)
    _advance_window(cid, transcript, reply)
    _store(cid, transcript, reply)

    # Synthesize the reply to a WAV and return it base64-encoded in the JSON. The phone client
    # decodes audio_wav_base64 -> bytes -> Blob({type:'audio/wav'}) and plays it.
    pcm, sr = v.synth_to_pcm(reply)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    audio_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"transcript": transcript, "reply": reply, "conversation_id": cid, "audio_wav_base64": audio_b64}

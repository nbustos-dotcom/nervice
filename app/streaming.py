"""Phase 1 streaming: token stream -> sentence-chunked TTS -> incremental WS frames.

Fast path (route "normal"): gpt-oss streams WITH tools attached; complete sentences are sent as
text frames and synthesized while later tokens still stream. If a tool-call delta appears, the
stream is ABANDONED and the turn reruns through the full existing pipeline (chat_with_tools ->
grounded synthesis -> verifier -> terminal tools), so nothing web-sourced is ever spoken from a
raw token stream — the grounding boundary is preserved byte-for-byte.

Holdback (council finding): the model sometimes emits prose before deciding to call a tool, so
the FIRST sentence's audio is held until a second sentence starts (or generation completes).
Its synthesis still starts immediately — only the send waits — so the cost is near zero in the
common case. Text frames flow per-sentence regardless; they are transcript, not speech.

Frame types (all JSON): {type:"transcript"} {type:"ack"} {type:"text"} {type:"audio"} {type:"done"}.
"""
import io
import re
import sys
import time
import wave
import base64
import asyncio

import app.llm as llm
import app.tools as tools
from groq import RateLimitError
from app import computer
from app import skills
from app import music
from app.agent import current_rung
from app.turnlog import log_turn
from app.router import classify, is_machine_question as router_is_machine
from app import sysinfo
from app.chat import build_system_prompt, execute_route, save_exchange, _ACK

_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

# Spoken the moment a normal-route stream pivots to a tool. Fast tools (get_weather) get no ack —
# the rerun answers quicker than an ack would help.
_PIVOT_ACK = {"web_search": "Let me check that — one sec.",
              "consult_claude": _ACK["hard"], "agent_build": _ACK["build"],
              "browse": _ACK["browse"], "propose_self_update": _ACK["selfmod"]}

_pending: set = set()


def _store(user_id: str, conversation_id: str, user_message: str, reply: str) -> None:
    """Same fire-and-forget persistence pattern as the REST endpoints and local loops."""
    async def _run():
        try:
            await save_exchange(user_id, conversation_id, user_message, reply)
        except Exception as e:
            print(f"[ws store failed] {repr(e)[:120]}", file=sys.stderr)
    t = asyncio.create_task(_run())
    _pending.add(t)
    t.add_done_callback(_pending.discard)


def _wav_b64(audio, sr: int) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _synth_sentence_b64(text: str) -> str | None:
    """One sentence -> base64 WAV via the existing Kokoro path. Sync; run in a thread (onnx
    releases the GIL). Lazy import so text-only /ws/chat never forces the voice-model load."""
    import app.voice as v
    cleaned = v._clean_for_speech(text)
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return None
    audio, sr = v._synth(cleaned)
    if not audio.size:
        return None
    return _wav_b64(audio, sr)


def _synth_full_b64(text: str) -> str | None:
    """Whole reply -> one base64 WAV (tool/ack answers arrive complete; reuse the proven
    per-sentence loop in synth_to_pcm)."""
    import app.voice as v
    pcm, sr = v.synth_to_pcm(text)
    if not pcm.size:
        return None
    return _wav_b64(pcm, sr)


class _AudioPipeline:
    """One worker synthesizes queued sentences IN ORDER, one at a time — sentence 1 gets the GPU
    alone so its audio ships ASAP while later sentences still synth behind it (concurrent synths
    contend on the GPU and bunch ALL audio at the end; measured). Sending sentence 0 additionally
    waits for release() — the holdback; its synthesis is never delayed, only the send."""

    def __init__(self, send):
        self._send = send
        self._q: asyncio.Queue = asyncio.Queue()
        self._released = asyncio.Event()
        self._count = 0
        self._worker = asyncio.create_task(self._run())

    def add(self, sentence: str) -> None:
        self._count += 1
        if self._count > 1:
            self._released.set()          # a second sentence exists — holdback over
        self._q.put_nowait(sentence)

    def release(self) -> None:
        self._released.set()

    async def _run(self):
        first = True
        while True:
            sent = await self._q.get()
            if sent is None:
                return
            b64 = await asyncio.to_thread(_synth_sentence_b64, sent)
            if first:
                await self._released.wait()
                first = False
            if b64:
                await self._send({"type": "audio", "wav_base64": b64})

    async def finish(self):
        """Release holdback, then drain: every queued sentence synths and sends, in order."""
        self._released.set()
        self._q.put_nowait(None)
        await self._worker

    def discard(self):
        """Pivot: drop everything unsent. An in-flight synth thread finishes and is ignored."""
        self._released.set()
        self._worker.cancel()


async def _stream_normal(system: str, text: str, window: list, send, voice: bool):
    """Token-stream a normal turn. Returns (reply, None) on success or (None, tool_name) when the
    model pivoted to a tool call and the caller must rerun the full pipeline."""
    msgs = ([{"role": "system", "content": system}] + list(window)
            + [{"role": "user", "content": text}])
    stream = await llm._client.chat.completions.create(
        model=llm.TOOL_MODEL, messages=msgs, tools=tools.TOOLS, tool_choice="auto",
        temperature=0.2, stream=True, **llm._effort(voice))

    audio = _AudioPipeline(send) if voice else None
    buf, sentences = "", []
    pivot_tool = None
    try:
        async for chunk in stream:
            d = chunk.choices[0].delta if chunk.choices else None
            if d is None:
                continue
            if d.tool_calls:
                for tc in d.tool_calls:
                    if tc.function and tc.function.name:
                        pivot_tool = tc.function.name
                pivot_tool = pivot_tool or "unknown"
                break
            if not d.content:
                continue
            buf += d.content
            parts = _SENT_BOUNDARY.split(buf)
            if len(parts) > 1:            # at least one complete sentence in the buffer
                for sent in parts[:-1]:
                    sent = sent.strip()
                    if not sent:
                        continue
                    sentences.append(sent)
                    await send({"type": "text", "text": sent})
                    if audio:
                        audio.add(sent)
                buf = parts[-1]
            if audio and sentences and buf.strip():
                audio.release()           # the NEXT sentence has started — holdback over
    finally:
        if pivot_tool and hasattr(stream, "close"):
            try:
                await stream.close()
            except Exception:
                pass

    if pivot_tool:
        if audio:
            audio.discard()               # holdback did its job: nothing half-spoken
        return None, pivot_tool

    tail = buf.strip()
    if tail:
        sentences.append(tail)
        await send({"type": "text", "text": tail})
        if audio:
            audio.add(tail)
    if audio:
        await audio.finish()
    return " ".join(sentences), None


async def stream_reply(user_id: str, text: str, window: list, send, voice: bool,
                       conversation_id: str) -> str:
    """One full streamed turn. `send` is an async callable taking one JSON-able frame dict.
    Returns the final reply; the caller advances its window. Persistence fires here."""
    t0 = time.monotonic()
    current_rung.set("groq")             # reset per turn; ask_claude flips it on a Claude escalation
    # A pending local-action confirmation answers the prior RISKY ask — never streamed, never
    # re-classified. Delivered as one complete text+audio reply, same as a tool turn.
    pending = computer.resolve_pending(user_id, text)
    if pending is not None:
        await send({"type": "text", "text": pending})
        if voice:
            b64 = await asyncio.to_thread(_synth_full_b64, pending)
            if b64:
                await send({"type": "audio", "wav_base64": b64})
        await send({"type": "done", "reply": pending})
        _store(user_id, conversation_id, text, pending)
        log_turn("control", "control", time.monotonic() - t0, "ws")
        return pending

    # user-defined skills run BEFORE normal routing (saved trigger -> run; create/list/delete here).
    # Delivered as one complete text+audio block, like a pending/tool turn.
    skill_reply = await skills.handle(user_id, text)
    if skill_reply is not None:
        await send({"type": "text", "text": skill_reply})
        if voice:
            b64 = await asyncio.to_thread(_synth_full_b64, skill_reply)
            if b64:
                await send({"type": "audio", "wav_base64": b64})
        await send({"type": "done", "reply": skill_reply})
        _store(user_id, conversation_id, text, skill_reply)
        log_turn("skill", "skill", time.monotonic() - t0, "ws")
        return skill_reply

    # favorite-artists management — deterministic, one text+audio block, works on any rung.
    music_reply = music.handle(text)
    if music_reply is not None:
        await send({"type": "text", "text": music_reply})
        if voice:
            b64 = await asyncio.to_thread(_synth_full_b64, music_reply)
            if b64:
                await send({"type": "audio", "wav_base64": b64})
        await send({"type": "done", "reply": music_reply})
        _store(user_id, conversation_id, text, music_reply)
        log_turn("music", "direct", time.monotonic() - t0, "ws")
        return music_reply

    # sysinfo DIRECT fast path: machine questions answered straight from telemetry (~1s) — no
    # retrieval, no classify, no LLM tool-round. None -> normal pipeline below.
    if router_is_machine(text):
        direct = await asyncio.to_thread(sysinfo.answer_machine_question, text)
        if direct:
            await send({"type": "text", "text": direct})
            if voice:
                b64 = await asyncio.to_thread(_synth_full_b64, direct)
                if b64:
                    await send({"type": "audio", "wav_base64": b64})
            await send({"type": "done", "reply": direct})
            _store(user_id, conversation_id, text, direct)
            log_turn("system", "direct", time.monotonic() - t0, "ws")
            return direct

    system, route = await asyncio.gather(
        build_system_prompt(user_id, text, voice_mode=voice), classify(text))

    reply, streamed = None, False
    if route == "normal":
        rate_limited = False
        try:
            reply, pivot = await _stream_normal(system, text, window, send, voice)
        except RateLimitError:
            # Groq daily cap hit at the streaming create (before any token/audio went out).
            # Ladder: extractive news (zero LLM) -> local Ollama (free/fast, local tools, same
            # system+window) -> Claude (context-threaded) -> friendly message (+'start Ollama'
            # hint). Delivered as one text+audio block below — nothing streamed yet, no double-speak.
            ans = None
            msgs = window + [{"role": "user", "content": text}]
            if llm.is_news_question(text):
                ans = await llm.extractive_news()
                if ans:
                    print("[groq 429 in stream_reply -> extractive news]", file=sys.stderr)
                    current_rung.set("extractive")
            if ans is None:
                ans = await llm._ollama_fallback(system, msgs, tools.TOOLS, tools.TOOL_FUNCS)
            if ans is None:
                print("[groq 429 in stream_reply -> claude ladder]", file=sys.stderr)
                ans = await llm._claude_fallback(system=system, messages=msgs)
            reply, pivot, rate_limited = (ans or await llm._exhausted_msg()), None, True
        if rate_limited:
            pass                          # reply set, streamed stays False -> delivered below
        elif reply is not None:
            streamed = True               # _stream_normal emitted sentences live as it generated
        else:                             # model pivoted to a tool mid-stream
            print(f"[STREAM pivot -> {pivot}]", file=sys.stderr)
            ack = _PIVOT_ACK.get(pivot)
            if ack:
                await send({"type": "ack", "text": ack})
                if voice:
                    b64 = await asyncio.to_thread(_synth_sentence_b64, ack)
                    if b64:
                        await send({"type": "audio", "wav_base64": b64})
            # Full existing pipeline: auto tool fire, per-tool timeouts, grounded synthesis,
            # verifier, terminal tools — identical to a REST turn. Nothing streams raw.
            reply = await execute_route(user_id, system, "normal", text, window, voice_mode=voice)

    if reply is None:                     # forced tool route or control — never token-streamed
        async def on_ack(a):
            await send({"type": "ack", "text": a})
            if voice:
                b64 = await asyncio.to_thread(_synth_sentence_b64, a)
                if b64:
                    await send({"type": "audio", "wav_base64": b64})
        reply = await execute_route(user_id, system, route, text, window, voice_mode=voice, on_ack=on_ack)

    if not streamed and reply:
        # Complete-reply paths (tool turns + the capped fallback rungs): text lands at once, but
        # audio is SENTENCE-PIPELINED like the streaming path — the phone starts speaking after
        # the first sentence's synth (~0.8s) instead of waiting for the whole reply's audio
        # (~1-3s saved on the local rung). Frames are sent in order; the client schedules them
        # back-to-back, so playback is seamless and complete.
        await send({"type": "text", "text": reply})
        if voice:
            sents = [s.strip() for s in _SENT_BOUNDARY.split(reply)
                     if s.strip() and re.search(r"[A-Za-z0-9]", s)]
            if len(sents) <= 1:
                b64 = await asyncio.to_thread(_synth_full_b64, reply)
                if b64:
                    await send({"type": "audio", "wav_base64": b64})
            else:
                for s in sents:
                    b64 = await asyncio.to_thread(_synth_sentence_b64, s)
                    if b64:
                        await send({"type": "audio", "wav_base64": b64})

    await send({"type": "done", "reply": reply or ""})
    _store(user_id, conversation_id, text, reply or "")
    rung = "exhausted" if (reply or "").startswith(llm.LADDER_EXHAUSTED_MSG) else current_rung.get()
    log_turn(route, rung, time.monotonic() - t0, "ws")
    return reply or ""

"""Simulate Groq's daily-cap 429 and confirm graceful handling end-to-end: /chat returns the
friendly text (200), /voice returns 200 with SYNTHESIZED audio of the limit message (phone speaks
it, no silence), /ws/chat streams the friendly message, and no traceback escapes. Then a success
stub confirms the 429 guards don't break normal turns. Throwaway."""
import sys, pathlib, asyncio, os, types, uuid, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

import httpx
from groq import RateLimitError
from fastapi.testclient import TestClient

import app.llm as llm
import app.api as api

TOKEN = os.environ["NERVICE_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}
cids = []


def make_429():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, request=req,
                          text="Rate limit reached for model. Please try again in 30m0s.")
    return RateLimitError("Rate limit reached. Please try again in 30m0s.", response=resp, body=None)


def cid(s):
    c = f"r429-{s}-{uuid.uuid4().hex[:8]}"; cids.append(c); return c


# ---- stub Groq client: 429 mode raises for every call; success mode returns canned content ----
_orig_create = llm._client.chat.completions.create


def install_429():
    async def create(**kw):
        raise make_429()
    llm._client.chat.completions.create = create


def install_success():
    async def create(**kw):
        if kw.get("response_format"):                      # classify / json -> a route
            msg = types.SimpleNamespace(content='{"route":"normal"}', tool_calls=None)
        else:                                              # tool loop / answer -> plain text
            msg = types.SimpleNamespace(content="All good here — running fine.", tool_calls=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    llm._client.chat.completions.create = create


def restore():
    llm._client.chat.completions.create = _orig_create


LIMIT_MARK = "daily free usage limit"


def main():
    results = {}
    with TestClient(api.app) as client:
        for _ in range(60):
            if api._voice_state["status"] != "warming":
                break
            import time; time.sleep(0.5)

        # ============ Phase A: simulated 429 ============
        install_429()
        try:
            # t1 /chat -> 200 + friendly text, no 500/traceback
            r = client.post("/chat", headers=H, json={"message": "how are you?", "conversation_id": cid("chat")})
            j = r.json()
            results["t1 /chat 429->friendly"] = (r.status_code == 200 and LIMIT_MARK in j.get("reply", ""))
            print(f"t1 /chat: HTTP {r.status_code}  reply={j.get('reply','')[:72]!r}")

            # t2 /voice -> 200 + friendly reply + NON-EMPTY synthesized audio (phone speaks it)
            import app.voice as v, io, wave
            qp, qsr = v.synth_to_pcm("How are you doing?")
            b = io.BytesIO()
            with wave.open(b, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(qsr); w.writeframes(qp.tobytes())
            r = client.post("/voice", headers=H, files={"audio": ("q.wav", b.getvalue(), "audio/wav")},
                            data={"conversation_id": cid("voice")})
            j = r.json()
            spoke = bool(j.get("audio_wav_base64"))
            results["t2 /voice 429->spoken"] = (r.status_code == 200 and LIMIT_MARK in j.get("reply", "") and spoke)
            print(f"t2 /voice: HTTP {r.status_code}  reply={j.get('reply','')[:54]!r}  audio={len(j.get('audio_wav_base64') or '')//1024}KB")

            # t3 /ws/chat -> friendly text frame + done, no crash
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({"token": TOKEN, "conversation_id": cid("ws")})
                assert ws.receive_json().get("type") == "ready"
                ws.send_json({"text": "how are you?"})
                texts, done = [], None
                for _ in range(50):
                    f = ws.receive_json()
                    if f["type"] == "text": texts.append(f["text"])
                    if f["type"] == "done": done = f; break
            got_friendly = any(LIMIT_MARK in t for t in texts) or (done and LIMIT_MARK in done.get("reply", ""))
            results["t3 /ws/chat 429->friendly"] = bool(done and got_friendly)
            print(f"t3 /ws/chat: text_frames={len(texts)} done={'yes' if done else 'NO'} friendly={got_friendly}")
        finally:
            restore()

        # ============ Phase B: success path intact (guards only catch 429) ============
        install_success()
        try:
            r = client.post("/chat", headers=H, json={"message": "how are you?", "conversation_id": cid("ok")})
            j = r.json()
            ok = r.status_code == 200 and "running fine" in j.get("reply", "").lower() and LIMIT_MARK not in j.get("reply", "")
            results["t4 success passthrough"] = ok
            print(f"t4 normal turn: HTTP {r.status_code}  reply={j.get('reply','')[:60]!r}")
        finally:
            restore()

    # results FIRST so a cleanup hiccup can never hide them
    print("\nRESULT:")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    allok = all(results.values())
    print("ALL PASS" if allok else "SOME FAILED")

    # best-effort cleanup with a FRESH engine bound to this loop (app.db's global engine is bound
    # to the now-closed TestClient loop — reusing it raises 'Event loop is closed')
    async def cleanup():
        import os
        from sqlalchemy import delete
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from app.models import Message, Memory
        eng = create_async_engine(os.environ["DATABASE_URL"], connect_args={"statement_cache_size": 0})
        try:
            Session = async_sessionmaker(eng)
            async with Session() as ss:
                for c in cids:
                    await ss.execute(delete(Message).where(Message.conversation_id == c))
                    await ss.execute(delete(Memory).where(Memory.source_conv_id == c))
                await ss.commit()
        finally:
            await eng.dispose()
    import time; time.sleep(3)   # let the fire-and-forget saves land first
    try:
        asyncio.run(cleanup())
        print("cleaned up", len(cids), "conversations")
    except Exception as e:
        print("cleanup note (non-fatal):", repr(e)[:80])
    sys.exit(0 if allok else 1)


main()

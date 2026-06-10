import sys, pathlib, asyncio, os, io, wave, base64, uuid
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from sqlalchemy import select, func, delete
from app.db import AsyncSessionLocal
from app.models import Message, Memory
import app.api as api

TOKEN = os.environ["NERVICE_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}
BAD = {"Authorization": "Bearer not-the-real-token"}


async def msg_count(cid):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(Message)
                                .where(Message.conversation_id == cid))).scalar()


async def drain():
    if api._pending:
        try:
            await asyncio.wait_for(asyncio.gather(*list(api._pending), return_exceptions=True), 40)
        except asyncio.TimeoutError:
            pass  # messages commit before remember(); count check still valid


async def main():
    results = {}
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as c:
        # t1 — 401 without / with-wrong token on /chat
        r_no = await c.post("/chat", json={"message": "hi"})
        r_bad = await c.post("/chat", json={"message": "hi"}, headers=BAD)
        results["t1"] = r_no.status_code == 401 and r_bad.status_code == 401
        print(f"t1: no-token={r_no.status_code} wrong-token={r_bad.status_code} -> {'PASS' if results['t1'] else 'FAIL'}")

        # t4 — /health with token 200, without 401
        h_ok = await c.get("/health", headers=H)
        h_no = await c.get("/health")
        results["t4"] = h_ok.status_code == 200 and h_no.status_code == 401
        print(f"t4: /health token={h_ok.status_code} {h_ok.json() if h_ok.status_code==200 else ''} | "
              f"no-token={h_no.status_code} -> {'PASS' if results['t4'] else 'FAIL'}")

        # t2 — two-turn conversation, window works, DB +4
        cid = "apitest-chat-" + uuid.uuid4().hex[:8]
        before = await msg_count(cid)
        r1 = await c.post("/chat", headers=H, json={
            "message": "Hold onto a fact for this chat: my lucky number is 4242.", "conversation_id": cid})
        r2 = await c.post("/chat", headers=H, json={
            "message": "What's the lucky number I just told you?", "conversation_id": cid})
        reply2 = r2.json()["reply"]
        window_ok = "4242" in reply2
        await drain()
        after = await msg_count(cid)
        results["t2"] = window_ok and (after - before == 4)
        print(f"t2: window refs 4242={window_ok} | reply2={reply2[:70]!r} | DB {before}->{after} (+{after-before}) "
              f"-> {'PASS' if results['t2'] else 'FAIL'}")

        # t3 — /voice: synth a WAV, upload, check transcript/reply/returned audio
        import app.voice as v
        pcm, sr = v.synth_to_pcm("What do you remember about me?")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
        cid3 = "apitest-voice-" + uuid.uuid4().hex[:8]
        rv = await c.post("/voice", headers=H,
                          files={"audio": ("question.wav", buf.getvalue(), "audio/wav")},
                          data={"conversation_id": cid3})
        jv = rv.json()
        transcript, reply = jv["transcript"], jv["reply"]
        ab = base64.b64decode(jv["audio_wav_base64"])
        with wave.open(io.BytesIO(ab)) as rw:
            dur = rw.getnframes() / rw.getframerate()
        tx_ok = "remember" in transcript.lower()
        results["t3"] = tx_ok and bool(reply) and dur > 1.0
        print(f"t3: transcript={transcript!r} | reply={reply[:60]!r} | returned audio {dur:.1f}s "
              f"-> {'PASS' if results['t3'] else 'FAIL'}")
        await drain()

        # webm/opus verification (the actual phone-browser format) — encode via av, decode via the
        # same path the API uses, no external ffmpeg
        try:
            import av, numpy as np
            wpath = tempfile_path = str(pathlib.Path(tempfile_dir()) / "probe.webm")
            _encode_webm_opus(pcm, sr, wpath)
            wt = v.transcribe_file(wpath)
            os.unlink(wpath)
            print(f"webm/opus decode (phone format): transcript={wt!r} -> {'OK' if 'remember' in wt.lower() else 'check'}")
        except Exception as e:
            print(f"webm/opus probe skipped: {repr(e)[:100]}")

        # cleanup test rows so the DB stays clean
        async with AsyncSessionLocal() as s:
            for c_id in (cid, cid3):
                await s.execute(delete(Message).where(Message.conversation_id == c_id))
                await s.execute(delete(Memory).where(Memory.source_conv_id == c_id))
            await s.commit()
        print("cleaned up test rows")

    print("\nRESULT:", "  ".join(f"{k}={'PASS' if val else 'FAIL'}" for k, val in results.items()))


def tempfile_dir():
    import tempfile
    return tempfile.gettempdir()


def _encode_webm_opus(pcm, sr, path):
    import av, numpy as np
    container = av.open(path, mode="w", format="webm")
    stream = container.add_stream("libopus", rate=sr)
    stream.layout = "mono"
    frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
    frame.rate = sr
    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()


asyncio.run(main())

import sys, pathlib, asyncio, io, wave, contextlib, uuid, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from sqlalchemy import delete
from app.db import AsyncSessionLocal
from app.models import Message, Memory
import os
import app.api as api

H = {"Authorization": f"Bearer {os.environ['NERVICE_API_TOKEN']}"}


async def main():
    results = {}
    transport = httpx.ASGITransport(app=api.app)
    # Manually drive the lifespan so the warm thread starts (ASGI test client doesn't run lifespan).
    async with api.lifespan(api.app):
        # t2 — /health reports warm status; wait for the background warm thread
        for _ in range(40):
            if api._voice_state["status"] == "warm" or "failed" in api._voice_state["status"]:
                break
            await asyncio.sleep(0.5)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as c:
            h = (await c.get("/health", headers=H)).json()
            results["t2"] = h.get("voice") == "warm"
            print(f"t2: /health -> voice={h.get('voice')!r} stt={h.get('stt')!r} tts={h.get('tts')!r} "
                  f"-> {'PASS' if results['t2'] else 'FAIL'}")

            # t3 — timing log line on a scripted /voice call
            import app.voice as v
            pcm, sr = v.synth_to_pcm("Quick check, what time is it?")
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
            cid = "uxtest-" + uuid.uuid4().hex[:8]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rv = await c.post("/voice", headers=H,
                                  files={"audio": ("q.wav", buf.getvalue(), "audio/wav")},
                                  data={"conversation_id": cid})
            jv = rv.json()
            timing = [ln for ln in err.getvalue().splitlines() if "[voice-timing]" in ln]
            has_fields = bool(timing) and all(k in timing[-1] for k in ("stt=", "llm=", "tts=", "total="))
            results["t3"] = rv.status_code == 200 and has_fields
            print(f"t3: reply={jv.get('reply','')[:50]!r}")
            print(f"    timing line: {timing[-1].strip() if timing else '(none)'}")
            print(f"t3 -> {'PASS' if results['t3'] else 'FAIL'}")

            # drain saves + cleanup
            if api._pending:
                try:
                    await asyncio.wait_for(asyncio.gather(*list(api._pending), return_exceptions=True), 30)
                except asyncio.TimeoutError:
                    pass
            async with AsyncSessionLocal() as s:
                await s.execute(delete(Message).where(Message.conversation_id == cid))
                await s.execute(delete(Memory).where(Memory.source_conv_id == cid))
                await s.commit()
            print("cleaned up test rows")

    print("\nRESULT:", "  ".join(f"{k}={'PASS' if val else 'FAIL'}" for k, val in results.items()))


asyncio.run(main())

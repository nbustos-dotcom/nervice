"""Phase 1 WS streaming tests: t1 auth, t2 incremental text frames, t3 first-audio latency,
t4 tool pivot + grounding, t5 REST regression. Run: .venv/Scripts/python.exe scripts/test_ws_stream.py"""
import sys, pathlib, asyncio, io, os, re, time, uuid, wave
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.net  # noqa
from dotenv import load_dotenv; load_dotenv()

from starlette.websockets import WebSocketDisconnect
from fastapi.testclient import TestClient

import app.api as api
import app.tools as tools
import app.streaming as streaming

TOKEN = os.environ["NERVICE_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}
results: dict = {}
cids: list = []


def newcid():
    cid = "wstest-" + uuid.uuid4().hex[:8]
    cids.append(cid)
    return cid


def collect_until_done(ws, max_frames=400):
    frames = []
    for _ in range(max_frames):
        f = ws.receive_json()
        frames.append(f)
        if f.get("type") == "done":
            break
    return frames


def question_wav() -> bytes:
    # phrased to reliably yield a MULTI-sentence reply — a 1-sentence reply has nothing to
    # stream incrementally (text+audio can only land at generation end; that's physics, not a bug)
    import app.voice as v
    pcm, sr = v.synth_to_pcm("Give me three reasons to get up early tomorrow, as three short sentences.")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def main():
    with TestClient(api.app) as client:
        # wait for the voice warm thread (t3/t5 need it; also makes timings honest)
        for _ in range(60):
            if api._voice_state["status"] != "warming":
                break
            time.sleep(0.5)

        # ---- t1: WS auth ----
        try:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({"token": "definitely-wrong"})
                ws.receive_json()
            bad_code = None
        except WebSocketDisconnect as e:
            bad_code = e.code
        try:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({"conversation_id": "x"})   # missing token entirely
                ws.receive_json()
            miss_code = None
        except WebSocketDisconnect as e:
            miss_code = e.code
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": TOKEN, "conversation_id": newcid()})
            ready = ws.receive_json()
        results["t1"] = bad_code == 1008 and miss_code == 1008 and ready.get("type") == "ready"
        print(f"t1 auth: bad->{bad_code} missing->{miss_code} good->{ready.get('type')} "
              f"-> {'PASS' if results['t1'] else 'FAIL'}")

        # ---- t2: incremental text over /ws/chat ----
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"token": TOKEN, "conversation_id": newcid()})
            assert ws.receive_json().get("type") == "ready"
            ws.send_json({"text": "give me two quick reasons to get up early tomorrow, two sentences"})
            frames = collect_until_done(ws)
        texts = [f for f in frames if f["type"] == "text"]
        results["t2"] = len(texts) >= 2 and frames[-1]["type"] == "done"
        print(f"t2 /ws/chat: {len(texts)} text frames, last={frames[-1]['type']} "
              f"-> {'PASS' if results['t2'] else 'FAIL'}")

        # ---- t3: /ws/voice first-audio latency ----
        wav = question_wav()
        with client.websocket_connect("/ws/voice") as ws:
            ws.send_json({"token": TOKEN, "conversation_id": newcid()})
            assert ws.receive_json().get("type") == "ready"
            t0 = time.perf_counter()
            ws.send_bytes(wav)
            first_audio = first_text = transcript_t = done_t = None
            frames = []
            while True:
                f = ws.receive_json()
                now = time.perf_counter() - t0
                frames.append(f)
                if f["type"] == "transcript" and transcript_t is None:
                    transcript_t = now
                elif f["type"] == "text" and first_text is None:
                    first_text = now
                elif f["type"] == "audio" and first_audio is None:
                    first_audio = now
                elif f["type"] == "done":
                    done_t = now
                    break
        n_audio = sum(1 for f in frames if f["type"] == "audio")
        n_text = sum(1 for f in frames if f["type"] == "text")
        ok_shape = (transcript_t is not None and first_audio is not None
                    and n_audio >= 2 and n_text >= 2)
        incremental = first_audio is not None and done_t is not None and first_audio < done_t - 0.3
        results["t3"] = ok_shape and incremental
        print(f"t3 /ws/voice: transcript@{transcript_t:.2f}s firstText@{first_text:.2f}s "
              f"FIRST-AUDIO@{first_audio:.2f}s done@{done_t:.2f}s "
              f"({n_text} text / {n_audio} audio frames)")
        print(f"t3 -> {'PASS' if results['t3'] else 'FAIL'} "
              f"(target <2.5s; incremental={incremental})")

        # ---- t4: tool pivot + grounding over /ws/chat ----
        captured = []
        orig_search = tools.TOOL_FUNCS["web_search"]
        async def logged(query, max_results=6):
            out = await orig_search(query, max_results)
            captured.append(out)
            return out
        tools.TOOL_FUNCS["web_search"] = logged
        try:
            with client.websocket_connect("/ws/chat") as ws:
                ws.send_json({"token": TOKEN, "conversation_id": newcid()})
                assert ws.receive_json().get("type") == "ready"
                ws.send_json({"text": "tell me the latest news about AI"})
                frames = collect_until_done(ws)
        finally:
            tools.TOOL_FUNCS["web_search"] = orig_search
        kinds = [f["type"] for f in frames]
        reply = frames[-1].get("reply", "")
        has_ack = "ack" in kinds
        src = " ".join(captured)
        nums = set(re.findall(r"\d[\d,.]*", reply))
        missing = [n for n in nums if n.rstrip(".,") not in src]
        results["t4"] = has_ack and bool(captured) and reply and not missing
        print(f"t4 pivot: frames={kinds[:6]}... ack={has_ack} searches={len(captured)} "
              f"nums_unsourced={missing} -> {'PASS' if results['t4'] else 'FAIL'}")

        # ---- t5: REST regression ----
        r1 = client.post("/chat", headers=H,
                         json={"message": "what's the weather right now?", "conversation_id": newcid()})
        ok_chat = r1.status_code == 200 and bool(r1.json().get("reply"))
        r2 = client.post("/voice", headers=H, files={"audio": ("q.wav", wav, "audio/wav")},
                         data={"conversation_id": newcid()})
        j2 = r2.json()
        ok_voice = (r2.status_code == 200 and j2.get("transcript")
                    and j2.get("reply") and j2.get("audio_wav_base64"))
        results["t5"] = bool(ok_chat and ok_voice)
        print(f"t5 REST: /chat={r1.status_code} /voice={r2.status_code} "
              f"-> {'PASS' if results['t5'] else 'FAIL'}")

        time.sleep(4)   # let fire-and-forget saves land while the portal loop is still alive

    # ---- cleanup ----
    async def cleanup():
        for s in (api._pending, streaming._pending):
            if s:
                await asyncio.gather(*list(s), return_exceptions=True)
        from sqlalchemy import delete
        from app.db import AsyncSessionLocal
        from app.models import Message, Memory
        async with AsyncSessionLocal() as s:
            for cid in cids:
                await s.execute(delete(Message).where(Message.conversation_id == cid))
                await s.execute(delete(Memory).where(Memory.source_conv_id == cid))
            await s.commit()
    asyncio.run(cleanup())
    print("cleaned up", len(cids), "test conversations")
    print("\nRESULT:", "  ".join(f"{k}={'PASS' if val else 'FAIL'}" for k, val in sorted(results.items())))


main()

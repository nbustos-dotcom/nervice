"""C-fix readback proof (SCRATCH) — exercises the REAL /memory/supersedes route in-process via
FastAPI TestClient: rejects a tokenless call (401), returns the logged supersede(s) with a bearer
token (200), and honors ?limit. No uvicorn / no voice-warm / no port fight. Falls back to a direct
route-fn call if TestClient/httpx is unavailable (the auth dep is the same shared gate as /pending)."""
import os
os.environ.setdefault("NERVICE_API_TOKEN", "proof-token")  # set BEFORE importing the app (read at import)
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import json, asyncio
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from app.api import app
TOK = os.environ["NERVICE_API_TOKEN"]


def show_event(e):
    print("  newest event: " + json.dumps({k: e.get(k) for k in ("ts", "old_content", "new_content", "old_id", "new_id", "salience")}, ensure_ascii=False))
    print(f"  candidates in event: {len(e.get('candidates', []))}  | all keys: {sorted(e.keys())}")


try:
    from fastapi.testclient import TestClient
    c = TestClient(app)  # no `with` -> skip lifespan (voice warm); the route only reads a file
    r0 = c.get("/memory/supersedes")
    r1 = c.get("/memory/supersedes", headers={"Authorization": f"Bearer {TOK}"})
    r2 = c.get("/memory/supersedes?limit=1", headers={"Authorization": f"Bearer {TOK}"})
    print("[real route via TestClient]")
    print(f"  GET /memory/supersedes  (no token) -> {r0.status_code}  (expect 401)")
    print(f"  GET /memory/supersedes  (bearer)   -> {r1.status_code}  (expect 200)")
    j1 = r1.json()
    print(f"  count={j1.get('count')}  events_returned={len(j1.get('events', []))}")
    if j1.get("events"):
        show_event(j1["events"][0])
    j2 = r2.json()
    print(f"  GET ...?limit=1 -> {r2.status_code}  count={j2.get('count')}  events_returned={len(j2.get('events', []))}  (expect <=1)")
except Exception as ex:
    print(f"[TestClient unavailable: {ex!r}] -> direct route-fn call (auth is the same shared gate as /pending)")
    from app.api import memory_supersedes
    out = asyncio.run(memory_supersedes(limit=20))
    print(f"  direct call: count={out.get('count')}  events_returned={len(out.get('events', []))}")
    if out.get("events"):
        show_event(out["events"][0])

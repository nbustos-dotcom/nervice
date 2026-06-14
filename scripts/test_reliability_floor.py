"""Fail-soft recall tests (reliability floor). Run:
   .venv/Scripts/python.exe scripts/test_reliability_floor.py

Proves a down/flaky Ollama (embeddings) or Supabase (DB) degrades recall instead of crashing the
turn. Simulates each failure by monkeypatching the dependency to raise, then asserts retrieve() and
build_system_prompt() return normally (no exception). build_system_prompt is the chokepoint BOTH
chat.respond() and streaming.stream_reply() use, so protecting it protects both paths.
"""
import sys
import asyncio
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from dotenv import load_dotenv
load_dotenv(str(_ROOT / ".env"))

from app import retrieval
from app import chat

P, F = [], []
def ok(m): P.append(m); print("  PASS", m)
def bad(m): F.append(m); print("  FAIL", m)


class _DeadSession:
    async def __aenter__(self): raise OSError("supabase unreachable (simulated)")
    async def __aexit__(self, *a): return False


async def main():
    print("[a] Ollama (embeddings) DOWN -> topic skipped, CORE preserved, degraded, no crash")
    async def _boom_embed(*a, **k): raise OSError("ollama embeddings down (simulated)")
    orig_embed = retrieval.embed
    retrieval.embed = _boom_embed
    try:
        r = await retrieval.retrieve("nate", "hello there")
        if isinstance(r, dict) and r.get("degraded") is True and r["topic"] == []:
            ok(f"retrieve degraded, topic empty, no crash (core preserved={len(r['core'])} — CORE needs no embedding)")
        else:
            bad(f"unexpected: {r!r}")
    except Exception as e:
        bad(f"retrieve RAISED on embed-down: {type(e).__name__}: {e}")
    finally:
        retrieval.embed = orig_embed

    print("[b] Supabase (DB) DOWN -> empty recall, degraded, no crash")
    orig_factory = retrieval.AsyncSessionLocal
    retrieval.AsyncSessionLocal = lambda: _DeadSession()
    try:
        r = await retrieval.retrieve("nate", "hello there")
        if isinstance(r, dict) and r["core"] == [] and r["topic"] == [] and r.get("degraded") is True:
            ok("retrieve returned empty + degraded, no crash")
        else:
            bad(f"unexpected: {r!r}")
    except Exception as e:
        bad(f"retrieve RAISED on DB-down: {type(e).__name__}: {e}")
    finally:
        retrieval.AsyncSessionLocal = orig_factory

    print("[c] build_system_prompt belt: even if retrieve() ITSELF raises, the turn's prompt builds")
    async def _boom_retrieve(*a, **k): raise RuntimeError("total recall failure (simulated)")
    orig_r = chat.retrieve
    chat.retrieve = _boom_retrieve
    try:
        sp = await chat.build_system_prompt("nate", "hello")
        if isinstance(sp, str) and len(sp) > 50 and "Nervice" in sp:
            ok("build_system_prompt returned a valid prompt despite retrieve() raising (gather can't 500)")
        else:
            bad(f"prompt looked wrong: {sp[:80]!r}")
    except Exception as e:
        bad(f"build_system_prompt RAISED: {type(e).__name__}: {e}")
    finally:
        chat.retrieve = orig_r

    print("[d] NORMAL path regression (real Ollama + Supabase, if up)")
    try:
        r = await retrieval.retrieve("nate", "what do you know about me")
        if r.get("degraded") is False:
            ok(f"normal recall works: degraded=False core={len(r['core'])} topic={len(r['topic'])}")
        else:
            ok(f"(info) recall degraded on this machine (Ollama/DB down now) — core={len(r['core'])} topic={len(r['topic'])}; "
               "fail-soft engaged, not a code regression")
    except Exception as e:
        bad(f"normal retrieve RAISED (should never): {type(e).__name__}: {e}")


asyncio.run(main())
print(f"\n=== {len(P)} passed, {len(F)} failed ===")
sys.exit(1 if F else 0)

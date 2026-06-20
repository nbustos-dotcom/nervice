"""COMMIT 1 proof (READ-ONLY, NO DB WRITE) — does raising _LOCAL_SALIENCE_CAP let a 4B-scored 4 reach
CORE? Scores facts on the 4B (the capped rung) and shows raw score vs min(raw, _LOCAL_SALIENCE_CAP) =
what would actually be stored. Run BEFORE the edit (cap=3) and AFTER (cap=4); the cap is imported live
so re-running a fresh process picks up the new value. Headline: 'building Nervice' (4B raw 4) is
stored 3 BEFORE (clamped, below CORE) -> 4 AFTER (reaches CORE). No DB writes."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from app.memory import RECONCILE_SYSTEM, _reconcile_user_msg, _LOCAL_SALIENCE_CAP
from app import ollama_client as ollama

FACTS = [
    ("Nate is building Nervice, a personal AI that remembers him across conversations.", "4 (headline)"),
    ("Nate is a college student.", "5 raw"),
    ("Nate likes The Strokes.", "2 (control, cap is a no-op)"),
]
N = 3


def raw_sal(r):
    if not isinstance(r, dict):
        return None
    c = [o for o in r.get("ops", []) if o.get("op") in ("add", "update", "supersede") and isinstance(o.get("salience"), int)]
    return max((o["salience"] for o in c), default=None)


async def score(fact):
    try:
        return raw_sal(await ollama.chat_json(RECONCILE_SYSTEM, _reconcile_user_msg("none", fact), timeout=45))
    except Exception as e:
        return f"ERR:{type(e).__name__}"


async def main():
    print(f"_LOCAL_SALIENCE_CAP = {_LOCAL_SALIENCE_CAP}   (4B-path stores min(raw, {_LOCAL_SALIENCE_CAP}); 70B uncapped)")
    if not await ollama.is_up():
        print("OLLAMA DOWN — cannot run.")
        return
    print(f"ollama up (model {ollama.OLLAMA_MODEL})\n")
    print(f"{'fact':46} {'expect':14} {'4B raw':12} {'stored=min(raw,cap)':20} {'reaches CORE(>=4)'}")
    print("-" * 108)
    for fact, exp in FACTS:
        raws = [await score(fact) for _ in range(N)]
        stored = [min(r, _LOCAL_SALIENCE_CAP) if isinstance(r, int) else r for r in raws]
        core = all(isinstance(s, int) and s >= 4 for s in stored)
        print(f"{fact[:44]:46} {exp:14} {str(raws):12} {str(stored):20} {core}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 120))

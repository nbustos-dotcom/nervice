"""COMMIT 2 proof (READ-ONLY, NO DB WRITE) — does the relationship-rubric fix score CLOSE relationships
4 while keeping INCIDENTAL people low? Scores facts on the 70B (the rung that scored Maddie 2) via the
LIVE RECONCILE_SYSTEM; run BEFORE and AFTER the rubric edit (only the salience-scale line changes). A
sha1 of the rubric is printed so each run proves which rubric produced it. No DB writes. Paced ~8s
between calls; ABORTS on a Groq cap (no retry, no grind -- 10-min-throttle history)."""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, hashlib
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from app.memory import RECONCILE_SYSTEM, _reconcile_user_msg
from app.llm import chat_json
from app import ollama_client as ollama
from groq import RateLimitError

RUNG = (sys.argv[1].lower() if len(sys.argv) > 1 else "70b")  # "70b" (default, headline rung) | "4b" (supplementary)

# (label, fact, expected-AFTER, kind)  kind: high=must be >=4 | low=must be <=2 or omit | ctrl=2-3 unchanged
CASES = [
    ("girlfriend (HEADLINE)",     "Nate's girlfriend is Maddie.",                 ">=4 (was 2)",   "high"),
    ("close family",              "Nate's mom is named Linda.",                   ">=4",           "high"),
    ("INCIDENTAL GUARD (barista)","Nate talked to a barista named Sam today.",    "<=2 or omit",   "low"),
    ("control (music taste)",     "Nate likes The Strokes.",                      "2-3 unchanged", "ctrl"),
]
N = 2


class Capped(Exception):
    pass


def prim(raw):
    if not isinstance(raw, dict):
        return (None, "ERR")
    c = [o for o in raw.get("ops", []) if o.get("op") in ("add", "update", "supersede") and isinstance(o.get("salience"), int)]
    if not c:
        return (None, "omit")
    o = max(c, key=lambda x: x.get("salience", 0))
    return (o["salience"], o["op"])


async def score(fact):
    msg = _reconcile_user_msg("none", fact)
    try:
        if RUNG == "4b":
            return prim(await ollama.chat_json(RECONCILE_SYSTEM, msg, timeout=45))
        return prim(await chat_json(RECONCILE_SYSTEM, msg))
    except RateLimitError as e:
        raise Capped(repr(e)[:80])
    except Exception as e:
        return (None, f"ERR:{type(e).__name__}")


def fmt(runs):
    return "/".join(str(s) if s is not None else op for s, op in runs)


def verdict(kind, runs):
    sals = [s for s, _ in runs if isinstance(s, int)]
    if kind == "high":
        return "PASS" if sals and all(s >= 4 for s in sals) else "FAIL (close relationship not reaching CORE)"
    if kind == "low":
        if any(isinstance(s, int) and s >= 4 for s, _ in runs):
            return "FAIL (incidental person reached CORE -- too broad, STOP)"
        if any(s == 3 for s in sals):
            return "PASS (note: a run scored 3 -- below CORE but not minimal)"
        return "PASS"
    if kind == "ctrl":
        return "PASS" if sals and all(2 <= s <= 3 for s in sals) else "FAIL (control drifted)"
    return "?"


async def main():
    fp = hashlib.sha1(RECONCILE_SYSTEM.encode("utf-8")).hexdigest()[:8]
    print(f"RELATIONSHIP RUBRIC TEST — rung={RUNG.upper()} — rubric=sha1:{fp} (len {len(RECONCILE_SYSTEM)})")
    print("=" * 96)
    first = True
    try:
        for label, fact, exp, kind in CASES:
            runs = []
            for _ in range(N):
                if not first and RUNG == "70b":
                    await asyncio.sleep(8)   # pace Groq only; 4B is local
                first = False
                runs.append(await score(fact))
            print(f"  {label:28} | {fmt(runs):12} | expect {exp:16} | {verdict(kind, runs)}")
    except Capped as c:
        print(f"\n  GROQ CAPPED ({c}) — proof DEFERRED (not faked). Partial above.")
        return 2
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(asyncio.wait_for(main(), 300)))

"""Capture-input gate (Part 2, Option A1) proof — BEFORE vs AFTER (user-message-only).

For each real degenerate exchange (inputs from the known-bad rows' source_snippets), score it two ways:
  AFTER  = app.memory._reconcile_user_msg(existing, user_text)  -> Nate's message ONLY; assistant NOT sent
  BEFORE = the OLD whole-exchange framing (User:.. / Assistant:.. both mined)
Scorer-only via chat_json, EXISTING=none, NO DB commit. 2 runs each (LLM variance). AFTER imports the
REAL shipped builder, so the proof reflects what remember() will do.

Self-paces through Groq caps: on a 429 it waits out the sticky window (capped_until) and resumes, so
all four cases complete. A global deadline aborts as INCOMPLETE rather than hang forever — a partial
run is never reported as conclusive.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, time
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from app.memory import RECONCILE_SYSTEM, _reconcile_user_msg
from app.llm import chat_json, groq_capped, capped_until, GROQ_MODEL

RUNS = 2
PACE = 12
DEADLINE_S = 50 * 60
class Capped(Exception): pass


def _old_msg(existing, u, a):
    return f"EXISTING related memories:\n{existing}\n\nLATEST exchange:\nUser: {u}\nAssistant: {a}"


async def _score_raw(msg):
    try:
        raw = await chat_json(RECONCILE_SYSTEM, msg)
    except Exception as e:
        b = repr(e).lower()
        if any(k in b for k in ("429", "rate", "capped", "sticky")): raise Capped(repr(e)[:80])
        raise
    return raw.get("ops", []) if isinstance(raw, dict) else []


async def score(msg, t0):
    """Score, waiting out Groq caps until success or the global deadline."""
    while True:
        if time.time() - t0 > DEADLINE_S:
            raise TimeoutError("global deadline exceeded before all cases ran")
        try:
            return await _score_raw(msg)
        except Capped:
            until = capped_until()
            wait = (until - time.time() + 5) if until else 60
            wait = max(10, min(wait, 11 * 60))
            print(f"      [Groq capped -> waiting {wait:.0f}s for the window to reset]", flush=True)
            await asyncio.sleep(wait)


def fmt(ops):
    if not ops: return "NOTHING (ops=[])"
    return "; ".join(f"{o.get('op')}({o.get('category')},s{o.get('salience')}): {(o.get('content') or '')[:50]}"
                     for o in ops if isinstance(o, dict))


# (label, user_text, assistant_text, expected)
CASES = [
    ("who-am-I recall summary", "who am I",
     "You're Nate-a college-aged software engineer, former hockey player, building Nervice and a "
     "micro-site business, Catholic, dating Maddie (who rides horses) and her dog Birdie.",
     "AFTER must capture NOTHING (assistant reply not sent; user side is just the question)"),
    ("tool-error build", "Create a note that my girlfriend's name is Maddie, and she's awesome. "
     "She likes riding horses, and she has a dog named Birdie.",
     "Build complete - here's what the builder did:\n\ntool error: Claude Code returned an error result: success",
     "AFTER: build/tool status not mined (it isn't sent); Nate's Maddie facts survive"),
    ("selfmod-proposal", "No, do not put the Y at the end of Maddie. It's just Maddie.",
     "Proposal 20260617-133001-438644 created: Adds Maddie's facts (M-A-D-D-I-E, full name Madeline, "
     "horse rider, dog Birdie) to the WHO NATE IS context in persona.py.",
     "AFTER: proposal status not mined (it isn't sent)"),
    ("POSITIVE CONTROL (clean Maddie)", "Create a note that my girlfriend's name is Maddie, "
     "and she's awesome. She likes riding horses, and she has a dog named Birdie.",
     "Done - I'll remember that about Maddie.",
     "AFTER must STILL capture the Maddie relationship fact (proves A1 is not over-dropping)"),
]


async def main() -> int:
    t0 = time.time()
    print(f"GATE PROOF (A1) — rung={GROQ_MODEL}  runs/framing={RUNS}  groq_capped={groq_capped()}", flush=True)
    try:
        for name, u, a, expect in CASES:
            print(f"\n{'='*94}\nCASE: {name}", flush=True)
            print(f"  expect: {expect}", flush=True)
            print(f"  User : {u!r}", flush=True)
            print(f"  Asst : {a[:86]!r}{'...' if len(a) > 86 else ''}", flush=True)
            print("  --- AFTER  (A1: user-message only; assistant NOT sent) ---", flush=True)
            for i in range(RUNS):
                print(f"      run{i+1}: {fmt(await score(_reconcile_user_msg('none', u), t0))}", flush=True)
                await asyncio.sleep(PACE)
            print("  --- BEFORE (whole-exchange, old framing) ---", flush=True)
            for i in range(RUNS):
                print(f"      run{i+1}: {fmt(await score(_old_msg('none', u, a), t0))}", flush=True)
                await asyncio.sleep(PACE)
        print(f"\n{'='*94}\nALL FOUR CASES COMPLETE", flush=True)
        return 0
    except TimeoutError as e:
        print(f"\n!!! INCOMPLETE — {e}. NOT all cases ran; do NOT treat as conclusive.", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

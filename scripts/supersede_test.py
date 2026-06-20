"""Over-supersede A-fix proof — does the reconcile rubric supersede correctly?

Scorer-only (chat_json + app.memory.RECONCILE_SYSTEM + _reconcile_user_msg), NO DB write. Feeds a
candidate set + a new statement and reports the op the 70B returns (supersede target / update / add /
noop). Run the SAME harness before and after the rubric edit; only RECONCILE_SYSTEM changes. A sha1
fingerprint of the rubric is printed so each report proves which rubric produced it.

  GOAL of the fix:
   - octarine (a distinct/TEST attribute) must NOT supersede the real purple row (add/noop).
   - 'now blue, not purple' and 'switched model' (genuine same-attribute changes) MUST still supersede
     (regression guards — the fix can't just disable supersede).
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, json, hashlib
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from app.memory import RECONCILE_SYSTEM, _reconcile_user_msg
from app.llm import chat_json, groq_capped

PURPLE = {"id": "2cf715a6-f67f-491e-9593-8e55b679dca2", "category": "preference", "salience": 3,
          "content": "Nate's favorite color is purple, but he publicly says it is blue"}
LLAMA = {"id": "11111111-1111-1111-1111-111111111111", "category": "preference", "salience": 3,
         "content": "Nate is using Llama 3.3 70B on Groq for Nervice's chat"}

# (label, candidate_rows, statement, expect, runs)  expect: "no-supersede" | "supersede"
CASES = [
    ("octarine — distinct/TEST attribute (must NOT supersede)", [PURPLE],
     "My favorite test color is octarine.", "no-supersede", 6),
    ("now-blue — genuine same-attribute change (MUST supersede)", [PURPLE],
     "Actually my favorite color is now blue, not purple.", "supersede", 2),
    ("model-switch — genuine change (MUST supersede)", [LLAMA],
     "I switched Nervice's chat model to gpt-oss-120b now, not Llama.", "supersede", 2),
]


class Capped(Exception): pass


async def score(existing_rows, statement):
    existing = json.dumps(existing_rows)
    msg = _reconcile_user_msg(existing, statement)
    try:
        raw = await chat_json(RECONCILE_SYSTEM, msg)
    except Exception as e:
        b = repr(e).lower()
        if any(k in b for k in ("429", "rate", "capped", "sticky")): raise Capped(repr(e)[:80])
        raise
    return raw.get("ops", []) if isinstance(raw, dict) else []


def classify(ops, cand_ids):
    """Did any op SUPERSEDE (or update) a candidate? Returns (verdict_str, supersedes_candidate_bool)."""
    sup = [o for o in ops if o.get("op") == "supersede" and o.get("id") in cand_ids]
    upd = [o for o in ops if o.get("op") == "update" and o.get("id") in cand_ids]
    if sup: return ("supersede->cand", True)
    if upd: return ("update->cand", True)   # also mutates the candidate — counts as "touched"
    if not ops: return ("noop", False)
    return ("add (new row)", False)


async def main() -> int:
    if groq_capped():
        print("GROQ CAPPED — A proof cannot run; build is done, proof DEFERRED (not faked)."); return 2
    fp = hashlib.sha1(RECONCILE_SYSTEM.encode("utf-8")).hexdigest()[:8]
    print(f"SUPERSEDE TEST — rubric=sha1:{fp} (len {len(RECONCILE_SYSTEM)})")
    print("=" * 92)
    for label, cands, stmt, expect, runs in CASES:
        cand_ids = {c["id"] for c in cands}
        print(f"\nCASE: {label}")
        print(f"  candidate: {cands[0]['content'][:60]!r}")
        print(f"  statement: {stmt!r}")
        results = []
        for i in range(runs):
            try:
                ops = await score(cands, stmt)
            except Capped as c:
                print(f"  run{i+1}: CAPPED ({c}) — DEFERRED, partial above"); return 2
            verdict, touched = classify(ops, cand_ids)
            results.append(touched)
            opsum = "; ".join(f"{o.get('op')}({o.get('id','')[:8] or '-'}) {(o.get('content') or '')[:40]!r}" for o in ops) or "(none)"
            print(f"    run{i+1}: {verdict:16} | {opsum}")
            if i < runs - 1: await asyncio.sleep(8)
        n_sup = sum(results)
        if expect == "no-supersede":
            ok = (n_sup == 0)
            print(f"  => {n_sup}/{runs} touched candidate | expect 0 | {'PASS' if ok else 'FAIL (still nuking purple)'}")
        else:
            ok = (n_sup == runs)
            print(f"  => {n_sup}/{runs} superseded candidate | expect {runs} | {'PASS' if ok else 'FAIL (genuine update lost!)'}")
    print("\n" + "=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

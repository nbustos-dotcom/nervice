"""Memory recall test — Part 1 proof harness.

Calls the REAL app.retrieval.retrieve() (live Ollama embeddings + live Supabase rows) for each
question in scripts/recall_fixture.json and reports PASS/FAIL on whether the expected substring
appears anywhere in the facts retrieve() actually returned. Pure read-only on the DB.

    .venv/Scripts/python.exe scripts/recall_test.py

Same script runs against unchanged retrieval (baseline) and the loosened retrieval (after); the
ONLY thing that changes between runs is app/retrieval.py. The PASS-count delta is the proof.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, json

# Windows console is cp1252 by default; fact content may contain non-ASCII (em dashes etc.).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.retrieval import retrieve, MAX_TOPIC_DISTANCE

FIXTURE = pathlib.Path(__file__).resolve().parent / "recall_fixture.json"


def _flatten(result: dict) -> list[tuple]:
    """(tier, category, salience, content) for every fact retrieve() returned, core first."""
    out = []
    for tier in ("core", "topic"):
        for m in result.get(tier, []):
            out.append((tier, m.category, m.salience, m.content))
    return out


async def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    user = data.get("user_id", "nate")
    cases = data["cases"]

    # surface the live thresholds so the report is self-describing (proves which retrieval ran)
    try:
        from app import retrieval as _r
        thresholds = (f"MAX_TOPIC_DISTANCE={_r.MAX_TOPIC_DISTANCE} "
                      f"CORE_MIN_SALIENCE={getattr(_r, 'CORE_MIN_SALIENCE', '==5 (identity/preference only)')} "
                      f"MAX_INJECTED_FACTS={getattr(_r, 'MAX_INJECTED_FACTS', 'n/a')}")
    except Exception:
        thresholds = f"MAX_TOPIC_DISTANCE={MAX_TOPIC_DISTANCE}"

    passed = 0
    blocks = []
    for c in cases:
        q, expect = c["q"], c["expect"]
        r = await retrieve(user, q)
        facts = _flatten(r)
        hay = " || ".join(content for (_, _, _, content) in facts).lower()
        ok = expect.lower() in hay
        passed += int(ok)

        lines = [f"Q: {q!r}",
                 f"   expect {expect!r}  ->  {'PASS' if ok else 'FAIL'}    "
                 f"(degraded={r.get('degraded')}  core={len(r.get('core', []))}  topic={len(r.get('topic', []))})"]
        for (tier, cat, sal, content) in facts:
            hit = "*" if expect.lower() in content.lower() else " "
            lines.append(f"     {hit}[{tier:5} {cat:12} s{sal}] {content[:108]}")
        if not facts:
            lines.append("     (no facts returned)")
        blocks.append("\n".join(lines))

    header = f"RECALL TEST  —  {passed}/{len(cases)} PASS   (user={user})"
    report = "\n".join([header, "=" * len(header), f"thresholds: {thresholds}", "", "\n\n".join(blocks)])
    print(report)
    return passed


if __name__ == "__main__":
    asyncio.run(main())

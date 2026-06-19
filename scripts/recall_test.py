"""Memory recall test — Part 1 proof harness.

Calls the REAL app.retrieval.retrieve() (live Ollama embeddings + live Supabase rows) for each
question in scripts/recall_fixture.json and reports PASS/FAIL on whether the expected substring
appears anywhere in the facts retrieve() actually returned. Read-only on the DB.

    .venv/Scripts/python.exe scripts/recall_test.py

The SAME script runs against unchanged retrieval (baseline) and the fixed retrieval (after); the
ONLY thing that changes between runs is app/retrieval.py. The PASS-count delta is the proof.

  - Cases marked "expected_fail" (a below-floor fact tracked for Part 2) report XFAIL when they fail
    as designed, or XPASS (a warning) if they unexpectedly pass — this keeps the seam visible.
  - "must_land_in_core" substrings assert the >=4 essentials actually survive the CORE cap instead of
    being silently truncated by updated_at ordering (recall must not hinge on edit recency).
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, json

# Windows console is cp1252 by default; fact content may contain non-ASCII (em dashes etc.).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import retrieval as _r
from app.retrieval import retrieve

FIXTURE = pathlib.Path(__file__).resolve().parent / "recall_fixture.json"


def _flatten(result: dict) -> list[tuple]:
    """(tier, category, salience, content) for every fact retrieve() returned, core first."""
    out = []
    for tier in ("core", "topic"):
        for m in result.get(tier, []):
            out.append((tier, m.category, m.salience, m.content))
    return out


def _thresholds() -> str:
    """Self-describing line so the report proves WHICH retrieval ran (pre- vs post-fix)."""
    return (f"MAX_TOPIC_DISTANCE={_r.MAX_TOPIC_DISTANCE}  "
            f"CORE_MIN_SALIENCE={getattr(_r, 'CORE_MIN_SALIENCE', 'n/a (==5 + identity/preference)')}  "
            f"CORE_LIMIT={getattr(_r, 'CORE_LIMIT', 'n/a (essentials_limit=3)')}  "
            f"TOPIC_LIMIT={getattr(_r, 'TOPIC_LIMIT', 'n/a (topic_limit=8)')}")


async def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    user = data.get("user_id", "nate")
    cases = data["cases"]
    must_land = data.get("must_land_in_core", [])

    npass = nfail = nxfail = nxpass = 0
    blocks = []
    for c in cases:
        q, expect = c["q"], c["expect"]
        xfail = bool(c.get("expected_fail"))
        r = await retrieve(user, q)
        facts = _flatten(r)
        hay = " || ".join(content for (_, _, _, content) in facts).lower()
        hit = expect.lower() in hay
        if xfail:
            status = "XFAIL (expected - Part-2 seam)" if not hit else "XPASS (!) seam closed unexpectedly"
            nxfail += int(not hit); nxpass += int(hit)
        else:
            status = "PASS" if hit else "FAIL"
            npass += int(hit); nfail += int(not hit)

        head = f"Q: {q!r}  [{c.get('type', '?')}]" + ("   EXPECTED-FAIL" if xfail else "")
        lines = [head,
                 f"   expect {expect!r}  ->  {status}    "
                 f"(core={len(r.get('core', []))} topic={len(r.get('topic', []))} degraded={r.get('degraded')})"]
        for (tier, cat, sal, content) in facts:
            mark = "*" if expect.lower() in content.lower() else " "
            lines.append(f"     {mark}[{tier:5} {cat:12} s{sal}] {content[:106]}")
        if not facts:
            lines.append("     (no facts returned)")
        blocks.append("\n".join(lines))

    # CORE landing check (truncation guard). CORE carries no cosine filter, so it is query-independent —
    # any query exposes the full essentials set. After the fix these MUST be present; at baseline they
    # are absent (CORE is salience==5 + identity/preference), which is the before/after contrast.
    r0 = await retrieve(user, "tell me about myself")
    core_txt = " || ".join(m.content for m in r0.get("core", [])).lower()
    land = [(sub, sub.lower() in core_txt) for sub in must_land]
    land_ok = all(ok for _, ok in land)
    land_lines = ["CORE LANDING (the >=4 essentials must survive the cap, not be truncated by recency):"]
    for sub, ok in land:
        land_lines.append(f"   {'OK     ' if ok else 'MISSING'}  '{sub}'  in CORE tier")
    land_lines.append(f"   CORE tier holds {len(r0.get('core', []))} fact(s): "
                      + " | ".join(m.content[:38] for m in r0.get("core", [])))

    real_total = npass + nfail
    header = (f"RECALL TEST  —  {npass}/{real_total} PASS   "
              f"(+{nxfail} XFAIL expected" + (f", {nxpass} XPASS!" if nxpass else "") + ")")
    report = "\n".join([header, "=" * len(header),
                        f"thresholds: {_thresholds()}", "",
                        "\n\n".join(blocks), "", "\n".join(land_lines)])
    print(report)
    # CI-usable exit code: nonzero on any unexpected fail, any XPASS surprise, or a missing CORE landing
    return 0 if (nfail == 0 and nxpass == 0 and land_ok) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

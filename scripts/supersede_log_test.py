"""C-fix proof (SCRATCH, self-cleaning) — does a REAL supersede write a full, reversible log entry,
and can a log failure ever break the supersede?

Snapshot-guarded externally (data/backups/memories_nate_20260619-supersede-C-proof.jsonl).
Net-zero on the DB: inserts ONE throwaway 'test-gizmo color' row, drives a genuine same-attribute
change through remember() (crimson -> teal, which the post-A rubric supersedes), inspects the
data/supersede_log.jsonl entry, proves failure-safety, then DELETES both throwaway rows. The LOG
entry is left in place on purpose (it's gitignored scratch + proves the readback) — note: it is a
TEST entry, clear data/supersede_log.jsonl if you want production to start clean.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, json
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

from sqlalchemy import text
from app.db import engine, AsyncSessionLocal
from app.models import Memory
from app.embeddings import embed
from app import memory as M

USER = "nate"
LOG = M._SUPERSEDE_LOG
CRIMSON = "Nate's test-gizmo color is crimson"
GIZMO_LIKE = "%test-gizmo%"


async def active_count(c):
    return (await c.execute(text("select count(*) from memories where user_id=:u and is_active=true"), {"u": USER})).scalar()


async def main():
    # 0. baseline
    async with engine.connect() as c:
        base = await active_count(c)
    print(f"baseline active: {base}")

    # 1. insert ONE throwaway crimson row (active)
    vec = await embed(CRIMSON)
    async with AsyncSessionLocal() as s:
        row = Memory(user_id=USER, content=CRIMSON, category="preference", salience=2, embedding=vec)
        s.add(row); await s.commit(); await s.refresh(row)
        crimson_id = str(row.id)
    print(f"inserted throwaway crimson: {crimson_id}")

    # 2. supersede-log size BEFORE the genuine turn
    before_lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
    print(f"supersede-log lines before: {len(before_lines)}")

    # 3. GENUINE same-attribute change via remember() (real reconcile path -> real supersede branch)
    ops = await M.remember(
        USER,
        "Actually my test-gizmo color is now teal -- it's no longer crimson.",
        "Got it -- teal it is.",
        source_conv_id="supersede-log-test",
    )
    print(f"remember() ops: {json.dumps(ops, ensure_ascii=False)}")
    superseded = any(o.get("op") == "supersede" for o in ops)
    print(f"  -> a supersede op fired: {superseded}")

    # 4. inspect the NEW supersede-log entries
    after_lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
    new = after_lines[len(before_lines):]
    print(f"\nsupersede-log NEW entries: {len(new)}")
    for ln in new:
        e = json.loads(ln)
        keys = ("ts", "old_id", "old_content", "new_id", "new_content", "salience", "category")
        print("  ENTRY: " + json.dumps({k: e.get(k) for k in keys}, ensure_ascii=False))
        cands = e.get("candidates", [])
        print(f"    candidates logged: {len(cands)} -> " + ", ".join(repr((c.get('content') or '')[:34]) for c in cands[:4]))
        full = set(("ts", "old_id", "old_content", "new_id", "new_content", "salience", "category", "candidates", "user_id", "conv"))
        print(f"    full-detail keys present: {sorted(full & set(e.keys())) == sorted(full)} ({sorted(e.keys())})")

    # 5. FAILURE-SAFETY: a broken log call must NEVER raise (candidates=None breaks entry-build inside the try)
    print("\nfailure-safety:")
    try:
        M._supersede_log(old_id="x", new_id="y", new_content="z", salience=4,
                         category="fact", candidates=None, user_id=USER, conv=None)
        print("  _supersede_log(candidates=None) returned WITHOUT raising -> OK (supersede can't be broken by logging)")
    except Exception as e:
        print(f"  _supersede_log RAISED (BAD): {e!r}")

    # 6. CLEANUP — clear FK then delete both throwaway rows (net-zero)
    async with engine.begin() as c:
        await c.execute(text("update memories set superseded_by=null where user_id=:u and lower(content) like :p"), {"u": USER, "p": GIZMO_LIKE})
        d = await c.execute(text("delete from memories where user_id=:u and lower(content) like :p"), {"u": USER, "p": GIZMO_LIKE})
        print(f"\ncleanup: deleted {d.rowcount} throwaway test-gizmo rows")
    async with engine.connect() as c:
        end = await active_count(c)
    print(f"active after cleanup: {end} (expect {base})  -> {'OK net-zero' if end == base else '!!! MISMATCH — investigate'}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 120))

"""Part 3, Step 2 — soft-deactivate the 3 known phantom rows.

These were laundered into the store by the Part-1 "who am I" self-ingestion tests (the assistant's
recall summary got re-captured as new facts). A1 (merged bb4b555) stopped NEW ones; these predate it.
Soft only (is_active=false) — NEVER delete. Snapshot-guarded: run scripts/memory_backup.py snapshot
first; scripts/memory_backup.py rollback <file> restores. Re-reads and confirms each row (content
matches + still active) before touching it; aborts rather than partial-deactivate if any drifted.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, collections
from sqlalchemy import text
from app.db import engine

USER = "nate"
# (id, expected content substring) — the 3 phantoms from the Part-3 diagnosis.
PHANTOMS = [
    ("c6ffb9b2-d97f-46c8-80b9-e9345711e61c", "software engineer"),
    ("9bf15509-bd6b-4132-ad41-57c7e72e9b40", "catholic"),
    ("7464985e-24f9-4729-9577-51d2c162f9bb", "building nervice"),
]


async def _active(c):
    rows = (await c.execute(text("select salience from memories where user_id=:u and is_active=true"), {"u": USER})).all()
    return len(rows), dict(sorted(collections.Counter(r[0] for r in rows).items()))


async def main():
    async with engine.connect() as c:
        print("=== BEFORE — the 3 phantom rows, live ===")
        confirmed = []
        for pid, sub in PHANTOMS:
            r = (await c.execute(text("select id, category, salience, is_active, content from memories "
                                      "where id=cast(:i as uuid) and user_id=:u"), {"i": pid, "u": USER})).first()
            if not r:
                print(f"  {pid}  NOT FOUND -> skip"); continue
            m = r._mapping
            fk = (await c.execute(text("select count(*) from memories where is_active=true and superseded_by=cast(:i as uuid)"), {"i": pid})).scalar()
            match = sub in (m["content"] or "").lower()
            print(f"  id={m['id']} [{m['category']} s{m['salience']} active={m['is_active']}] "
                  f"FK-target-of-active={fk} content_matches={match}\n      {m['content'][:72]!r}")
            if m["is_active"] and match:
                confirmed.append(pid)
            else:
                print(f"      -> SKIP (active={m['is_active']}, matches={match})")
        before_n, before_d = await _active(c)
        print(f"  active before: {before_n}  dist {before_d}")

    if len(confirmed) != 3:
        print(f"\n!!! Only {len(confirmed)}/3 confirmed — NOT proceeding (no partial deactivate). Investigate drift.")
        return

    async with engine.begin() as c:
        res = await c.execute(text("update memories set is_active=false where id::text = any(:ids) and user_id=:u and is_active=true"),
                              {"ids": confirmed, "u": USER})
        print(f"\n=== soft-deactivated {res.rowcount} rows (is_active=false; NOT deleted) ===")

    async with engine.connect() as c:
        print("=== AFTER — the 3 rows ===")
        for pid, _ in PHANTOMS:
            r = (await c.execute(text("select salience, is_active, content from memories where id=cast(:i as uuid)"), {"i": pid})).first()
            print(f"  {pid} active={r[1]} s{r[0]} :: {r[2][:48]!r}")
        after_n, after_d = await _active(c)
        print(f"  active after: {after_n} (before {before_n}, delta {after_n - before_n})  dist {after_d}")


if __name__ == "__main__":
    asyncio.run(main())

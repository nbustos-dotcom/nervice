"""Part 3 Step 3 EXECUTE — soft-deactivate the 29 Nate-approved prune ids (EXACT id list only;
no bucketing, no pattern-matching). Re-reads + confirms each row (content matches + still active);
ABORTS with no write if any fails. Soft only (is_active=false), never deletes. One all-or-nothing
transaction: requires exactly 29 rows flipped, else raises and rolls back.

Snapshot-guarded (rollback point):
  data/backups/memories_nate_20260619-step3-execute.jsonl
Rollback:
  .venv/Scripts/python.exe scripts/memory_backup.py rollback data/backups/memories_nate_20260619-step3-execute.jsonl
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, collections
from sqlalchemy import text
from app.db import engine

USER = "nate"
# (id, distinctive lowercase content substring) — the 29 approved ids.
APPROVED = [
    ("7b9e9d2e-e1c2-42d9-867a-18a635be08d9", "free tiers for service (the ai assistant)"),
    ("b28d63e4-a8f4-43b5-a976-47866a317670", "free tiers for service (the ai assistant)"),
    ("d3e499c6-a7bb-4cbb-91ae-4ef58d2fb2f9", "spiritual significance of eucharistic miracles"),
    ("34e40917-b23e-4ba4-b3d4-0343914b1900", "listen to music other than the strokes"),
    ("836419a3-3836-492b-8bd7-fdea9b3d09cd", "intersection of tech and faith"),
    ("6efee9d3-540c-44aa-bb78-4cee0395c099", "closed-loop orchestrator demo app"),
    ("bf0b2548-f988-43d1-84d0-ae0f44502423", "interested in computer science"),
    ("5dc4fce3-d0c4-4723-a847-f6583c5cbb9c", "'1 hour lo-fi study playlist' as a command"),
    ("7d7c5351-97be-4d84-9583-b5271716b0d9", "'play music by the back seat lovers' as a command"),
    ("5089a926-cc71-4642-8c1e-dc1a4e728e83", "'play music' as a command to open youtube and play music"),
    ("4c85dcd2-485b-48a3-bd47-5b1ed44dba2d", "'play silence' as a command"),
    ("5beba4ed-cd02-48f8-978c-8d060970acb0", "'play som musik' as a command"),
    ("bae9ed96-a75f-429f-9ced-156a2433a969", "'play some banger music' as a command"),
    ("f2f9ae94-bcbd-46b7-84bd-dc74d94a344a", "'play something completely different' as a command"),
    ("523d5478-52be-4329-8aa4-dbcf57575d84", "'play something else' as a command"),
    ("32d68727-76ff-4e58-8527-e7d65daa5fb1", "'play something productive' as a command"),
    ("118ea756-f981-440d-8544-0ca5c84be5e4", "'.' as a command to trigger status checks"),
    ("10d5da09-9bca-46f1-96d7-8898344ce3c2", "'cap test' as a command"),
    ("e38a696c-d882-42f1-8856-746ba5bfc13e", "'open example.com' as a command"),
    ("8aa26d3e-3de5-4a0d-92af-f5ec83c802d9", "'test ping' as a command"),
    ("adbdcc29-81d9-4d1c-8471-ba88ce9482c5", "lab 9 for sat2343 due tomorrow"),
    ("b59b0047-e68e-4cff-b12d-55cd89f0b38f", "quiz 4 for sat2343 due tomorrow"),
    ("ce6d195b-9255-4d37-a912-ebd9a3290e8f", "subnet quiz 7 for sat2343 due tomorrow"),
    ("9e860763-7600-40bc-83bb-efedf344987c", "assignment 5 for cs2321 due june 14"),
    ("904045dd-6f28-4555-8cbc-345b7e528654", "assignment 6 for cs2321 due tomorrow"),
    ("1ff155f7-4f81-4c2f-bf33-ef8a571640e6", "assignment for cs2321 due tomorrow on canvas"),
    ("b3c42097-0dd9-4607-9ded-1bf483dd9eda", "nate is 19 years old."),
    ("2375b282-eabd-4198-8cba-0929d5aa237f", "pomodoro project with fixed 1490-second"),
    ("0ce906e6-044f-40dc-954c-e64460fa5386", "service-loop' in the workspace directory c:/users/nateb/loop-page-test"),
]
KEEPS = [  # the 6 Bucket-A dedup survivors + the kept age row — must stay ACTIVE
    "5886393b-4fa2-454a-8a96-ea8304d038e6", "1e828617-5019-4a26-8a2e-2b3d5160b614",
    "2d10810c-b225-4e5c-a4ff-5865e0e4584c", "5dff7d50-3ea7-4189-bc16-ffd19ef756ae",
    "c40531cd-fdf6-4bbc-914f-9be39ca9b383", "cfa86dbe-ba7f-41ef-a8c6-7295b831575c",
    "ecee91b8-7c71-4b47-a655-01c222977c32",
]
IDS = [i for i, _ in APPROVED]


async def active_summary(c):
    rows = (await c.execute(text("select salience from memories where user_id=:u and is_active=true"), {"u": USER})).all()
    return len(rows), dict(sorted(collections.Counter(r[0] for r in rows).items()))


async def main():
    assert len(APPROVED) == 29, f"expected 29 approved ids, got {len(APPROVED)}"
    async with engine.connect() as c:
        print("=== CONFIRM TABLE (29 approved ids) ===")
        print(f"  {'id':38} active match")
        all_ok = True
        for pid, sub in APPROVED:
            r = (await c.execute(text("select content, is_active from memories where id=cast(:i as uuid) and user_id=:u"), {"i": pid, "u": USER})).first()
            if not r:
                print(f"  {pid}  NOT-FOUND"); all_ok = False; continue
            active = r._mapping["is_active"]; match = sub in (r._mapping["content"] or "").lower()
            if not (active and match): all_ok = False
            print(f"  {pid}  {('Y' if active else 'N'):6} {('Y' if match else 'N')}   {r._mapping['content'][:44]!r}")
        before_n, before_d = await active_summary(c)
        print(f"\n  pre-write active: {before_n}  dist {before_d}")

    if not all_ok:
        print("\n!!! ABORT — not all 29 confirmed (active=Y + match=Y). NO write performed.")
        return
    if before_n != 86:
        print(f"\n!!! ABORT — pre-write active is {before_n}, expected 86. NO write (investigate).")
        return

    async with engine.begin() as c:
        res = await c.execute(text("update memories set is_active=false where id::text = any(:ids) and user_id=:u and is_active=true"), {"ids": IDS, "u": USER})
        if res.rowcount != 29:
            raise RuntimeError(f"expected 29 flipped, got {res.rowcount} — rolling back (all-or-nothing)")
        print(f"\n=== soft-deactivated {res.rowcount} rows in one transaction (all-or-nothing) ===")

    async with engine.connect() as c:
        after_n, after_d = await active_summary(c)
        still = (await c.execute(text("select count(*) from memories where id::text = any(:ids) and is_active=true"), {"ids": IDS})).scalar()
        keeps = (await c.execute(text("select count(*) from memories where id::text = any(:ids) and is_active=true"), {"ids": KEEPS})).scalar()
    print(f"  post-write active: {after_n}  dist {after_d}")
    print(f"  delta {after_n - before_n} (expect -29) | of the 29 still active: {still} (expect 0) | of 7 KEEPS active: {keeps} (expect 7)")
    ok = (after_n == 57 and still == 0 and keeps == 7)
    print(f"\n  RESULT: {'OK — exactly 29 flipped, 57 active, keeps intact' if ok else '!!! PROBLEM — STOP and roll back'}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 60))

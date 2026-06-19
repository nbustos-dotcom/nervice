"""Part 3 Step 4 EXECUTE (Option A) — promote/reactivate/demote/consolidate, EXACT ids only.
One all-or-nothing transaction; every step asserts its rowcount or the whole thing rolls back.
Re-reads + confirms each target's pre-state first; aborts with no write on any mismatch.

Snapshot/rollback point:
  data/backups/memories_nate_20260619-step4-execute.jsonl
Rollback:
  .venv/Scripts/python.exe scripts/memory_backup.py rollback data/backups/memories_nate_20260619-step4-execute.jsonl
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, collections
from sqlalchemy import text
from app.db import engine

USER = "nate"
NERVICE = "f06904f9-928c-4cba-9259-de34587088e1"
CATHOLIC = "9bf15509-bd6b-4132-ad41-57c7e72e9b40"
PROMOTE = [("6bb5167e-bb8a-4ca5-850e-decbe3cb8b7d", 2, "Maddie"),
           ("e6cee05a-cfcd-44da-8743-cdbbf8f780c0", 3, "micro-site/AI-teacher combined")]   # cur salience -> 4
REACT = [(NERVICE, "Building Nervice (stays s4)"), (CATHOLIC, "Catholic (set s4)")]           # inactive s4 -> active
DEMOTE = [("0f0ba4a6-c770-439b-b897-3be4f4706d69", "Hentai"),
          ("131cda23-0d25-48c9-a019-b21bb7108990", "declutter"),
          ("4eaca6d3-a3de-4413-ad13-abf193844bda", "Claude Corps")]                           # s4 active -> s3
CONSOL = [("018ca14b-cd48-4e3c-b000-cf19a7ea65af", "micro-site clean"),
          ("c40531cd-fdf6-4bbc-914f-9be39ca9b383", "micro-site demo-app"),
          ("d2c48b93-8997-4a93-acc9-6e368ff44fb3", "micro-site MVP status")]                  # s3 active -> inactive
UNTOUCHED = [("6c3ba00e-ac00-4aba-926d-aa36134ce08e", "Birdie"),
             ("f65a3830-099f-41c6-a002-0455138bf8f8", "horses")]
EXPECTED_CORE = {"647ce83c-2692-44f0-b641-c62cb64ee608", "44af3bc9-e12b-4532-988b-ef7c19cb9a07",
                 "3e10b4ba-e036-484f-80c3-f9af393203d8", "6bb5167e-bb8a-4ca5-850e-decbe3cb8b7d",
                 NERVICE, CATHOLIC, "e6cee05a-cfcd-44da-8743-cdbbf8f780c0"}


async def active_summary(c):
    rows = (await c.execute(text("select salience from memories where user_id=:u and is_active=true"), {"u": USER})).all()
    return len(rows), dict(sorted(collections.Counter(r[0] for r in rows).items()))


async def read1(c, pid):
    return (await c.execute(text("select salience, is_active, superseded_by, content from memories where id=cast(:i as uuid) and user_id=:u"), {"i": pid, "u": USER})).first()


async def main():
    async with engine.connect() as c:
        print("=== CONFIRM TABLE ===")
        ok = True
        async def chk(pid, action, cond_fn):
            nonlocal ok
            r = await read1(c, pid)
            if r is None:
                print(f"  {pid[:8]} NOT-FOUND | {action}"); ok = False; return
            m = r._mapping; good = cond_fn(m)
            ok = ok and good
            print(f"  {pid[:8]} [s{m['salience']} active={m['is_active']} supby={'set' if m['superseded_by'] else 'null'}] "
                  f"{m['content'][:38]!r} | {action} | {'OK' if good else 'MISMATCH'}")
        for pid, cs, lab in PROMOTE:
            await chk(pid, f"PROMOTE s{cs}->4 ({lab})", lambda m, cs=cs: m['salience'] == cs and m['is_active'])
        for pid, lab in REACT:
            await chk(pid, f"REACTIVATE ({lab})", lambda m: (not m['is_active']) and m['salience'] == 4)
        for pid, lab in DEMOTE:
            await chk(pid, f"DEMOTE s4->3 ({lab})", lambda m: m['salience'] == 4 and m['is_active'])
        for pid, lab in CONSOL:
            await chk(pid, f"CONSOLIDATE deactivate ({lab})", lambda m: m['is_active'] and m['salience'] == 3)
        for pid, lab in UNTOUCHED:
            await chk(pid, f"UNTOUCHED ({lab})", lambda m: m['is_active'])
        before_n, before_d = await active_summary(c)
        print(f"\n  pre-write active: {before_n}  dist {before_d}")

    if not ok:
        print("\n!!! ABORT — confirm mismatch above. NO write performed."); return
    if before_n != 57:
        print(f"\n!!! ABORT — pre-write active {before_n} != 57. NO write."); return

    async with engine.begin() as c:
        total = 0
        for pid, cs, lab in PROMOTE:
            r = await c.execute(text("update memories set salience=4 where id=cast(:i as uuid) and user_id=:u and salience=:cs and is_active=true"), {"i": pid, "u": USER, "cs": cs})
            if r.rowcount != 1: raise RuntimeError(f"promote {pid} rowcount {r.rowcount}")
            total += r.rowcount
        r = await c.execute(text("update memories set is_active=true, superseded_by=null where id=cast(:i as uuid) and user_id=:u and is_active=false"), {"i": NERVICE, "u": USER})
        if r.rowcount != 1: raise RuntimeError(f"reactivate Nervice rowcount {r.rowcount}")
        total += r.rowcount
        r = await c.execute(text("update memories set is_active=true, salience=4, superseded_by=null where id=cast(:i as uuid) and user_id=:u and is_active=false"), {"i": CATHOLIC, "u": USER})
        if r.rowcount != 1: raise RuntimeError(f"reactivate Catholic rowcount {r.rowcount}")
        total += r.rowcount
        r = await c.execute(text("update memories set salience=3 where id::text = any(:ids) and user_id=:u and salience=4 and is_active=true"), {"ids": [i for i, _ in DEMOTE], "u": USER})
        if r.rowcount != 3: raise RuntimeError(f"demote rowcount {r.rowcount}")
        total += r.rowcount
        r = await c.execute(text("update memories set is_active=false where id::text = any(:ids) and user_id=:u and is_active=true"), {"ids": [i for i, _ in CONSOL], "u": USER})
        if r.rowcount != 3: raise RuntimeError(f"consolidate rowcount {r.rowcount}")
        total += r.rowcount
        if total != 10: raise RuntimeError(f"total rowcount {total} != 10 — rolling back")
        print(f"\n=== applied {total} row-ops in one all-or-nothing transaction ===")

    async with engine.connect() as c:
        core = (await c.execute(text("select id, salience, content from memories where user_id=:u and is_active=true and salience>=4 order by salience desc, content"), {"u": USER})).all()
        core_ids = {str(r._mapping['id']) for r in core}
        after_n, after_d = await active_summary(c)
        print("=== NEW CORE (active salience>=4) ===")
        for r in core:
            m = r._mapping; print(f"   {str(m['id'])[:8]} [s{m['salience']}] {m['content'][:55]}")
        print(f"\n  CORE count: {len(core)} (expect 7) | exact expected set: {core_ids == EXPECTED_CORE}")
        if core_ids != EXPECTED_CORE:
            print("   unexpected diff:", core_ids ^ EXPECTED_CORE)
        for pid, lab in DEMOTE:
            m = (await read1(c, pid))._mapping; print(f"  demoted {pid[:8]} ({lab}): s{m['salience']} active={m['is_active']}  (expect s3 active=True)")
        for pid, lab in CONSOL:
            m = (await read1(c, pid))._mapping; print(f"  consolidated {pid[:8]} ({lab}): active={m['is_active']}  (expect False)")
        for pid, lab in UNTOUCHED:
            m = (await read1(c, pid))._mapping; print(f"  untouched {pid[:8]} ({lab}): s{m['salience']} active={m['is_active']}  (expect s2 active=True)")
        print(f"\n  active: {before_n} -> {after_n}  (expect 56)  dist {after_d}")
        good = (len(core) == 7 and core_ids == EXPECTED_CORE and after_n == 56)
        print(f"\n  RESULT: {'OK — CORE is the exact 7, active 56' if good else '!!! PROBLEM — STOP and roll back'}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 60))

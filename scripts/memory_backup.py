"""Memory snapshot / rollback infra (Part 3, Step 1) — the safety net every Part-3 write depends on.

  snapshot <path>   dump nate's rows (active AND inactive: id, salience, is_active, superseded_by,
                    content, category) to a JSONL FILE (PRIMARY, DB-independent) + (re)create the
                    memories_bak table (secondary). Prints count, active count, salience distribution,
                    and a state fingerprint.
  fingerprint       print count, active count, salience distribution, and a sha256 state fingerprint
                    over sorted (id|salience|is_active|superseded_by) — byte-identical state => same hash.
  rollback <path>   restore salience / is_active / superseded_by per-row, by id, from a snapshot file.

The FILE is the trusted snapshot (survives a DB-level accident); memories_bak is a convenience copy.
Read-only except snapshot (writes a file + the bak table) and rollback (per-row UPDATE). NEVER deletes.
"""
import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, json, hashlib, collections
from sqlalchemy import text
from app.db import engine

USER = "nate"


async def _rows(c):
    return (await c.execute(text(
        "select id, salience, is_active, superseded_by, content, category "
        "from memories where user_id=:u order by id"), {"u": USER})).all()


def _fingerprint(rows):
    h = hashlib.sha256()
    for r in rows:
        m = r._mapping
        h.update(f"{m['id']}|{m['salience']}|{m['is_active']}|{m['superseded_by']}\n".encode())
    return h.hexdigest()[:16]


def _summary(rows):
    active = [r for r in rows if r._mapping["is_active"]]
    dist = dict(sorted(collections.Counter(r._mapping["salience"] for r in active).items()))
    return f"total={len(rows)} active={len(active)} dist={dist} fp={_fingerprint(rows)}"


async def cmd_fingerprint():
    async with engine.connect() as c:
        print(" ", _summary(await _rows(c)))


async def cmd_snapshot(path):
    async with engine.connect() as c:
        rows = await _rows(c)
    fp = pathlib.Path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        for r in rows:
            m = r._mapping
            f.write(json.dumps({
                "id": str(m["id"]), "salience": m["salience"], "is_active": m["is_active"],
                "superseded_by": str(m["superseded_by"]) if m["superseded_by"] else None,
                "content": m["content"], "category": m["category"]}, ensure_ascii=False) + "\n")
    print(f"  FILE snapshot -> {fp}  (PRIMARY, trusted)")
    try:
        async with engine.begin() as c:
            await c.execute(text("drop table if exists memories_bak"))
            await c.execute(text(
                f"create table memories_bak as select id, salience, is_active, superseded_by, "
                f"content, category from memories where user_id='{USER}'"))
        print("  memories_bak table (re)created (secondary)")
    except Exception as e:
        print(f"  [memories_bak skipped: {repr(e)[:80]}] — FILE snapshot is the primary, proceed")
    print(" ", _summary(rows))


async def cmd_rollback(path):
    data = [json.loads(l) for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    n = 0
    async with engine.begin() as c:
        for d in data:
            res = await c.execute(text(
                "update memories set salience=:s, is_active=:a, superseded_by=cast(:sb as uuid) "
                "where id=cast(:id as uuid) and user_id=:u"),
                {"s": d["salience"], "a": d["is_active"], "sb": d["superseded_by"], "id": d["id"], "u": USER})
            n += res.rowcount
    print(f"  rollback: {n} rows restored from {path}")
    await cmd_fingerprint()


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "fingerprint"
    if cmd == "snapshot":
        await cmd_snapshot(sys.argv[2])
    elif cmd == "rollback":
        await cmd_rollback(sys.argv[2])
    else:
        await cmd_fingerprint()


if __name__ == "__main__":
    asyncio.run(main())

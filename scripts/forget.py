import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.db import AsyncSessionLocal
from app.models import Memory
from sqlalchemy import select


async def main():
    substr = " ".join(sys.argv[1:]).strip()
    if not substr:
        print("usage: python scripts/forget.py <text substring>")
        return
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(Memory).where(
            Memory.user_id == "nate", Memory.is_active == True, Memory.content.ilike(f"%{substr}%")  # noqa: E712
        ))).scalars().all()
        for m in rows:
            print(f"forgetting: [{m.category} s{m.salience}] {m.content}")
            m.is_active = False
        await s.commit()
        print(f"{len(rows)} forgotten.")


asyncio.run(main())

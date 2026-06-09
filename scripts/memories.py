import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.db import AsyncSessionLocal
from app.models import Memory
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Memory).where(Memory.user_id == "nate", Memory.is_active == True).order_by(Memory.salience.desc())  # noqa: E712
        )).scalars().all()
    print(f"NERVICE KNOWS ({len(rows)}):")
    for m in rows:
        print(f"  [{m.category} s{m.salience}] {m.content}")


asyncio.run(main())

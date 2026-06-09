import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio, json
from app.memory import remember
from app.db import AsyncSessionLocal
from app.models import Memory
from sqlalchemy import select

USER = "nate"


async def main():
    applied = await remember(
        USER,
        "I'm building Nervice, a personal AI that remembers me across conversations. I want everything free - Groq, Ollama, Supabase free tiers, no paid services.",
        "Understood - free-first stack, persistent memory is the core of Nervice.",
    )
    print("OPS APPLIED:", json.dumps(applied, indent=2))
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(select(Memory).where(Memory.user_id == USER, Memory.is_active == True))  # noqa: E712
        ).scalars().all()
        print(f"\nMEMORIES IN DB ({len(rows)}):")
        for m in rows:
            print(f"  [{m.category} s{m.salience}] {m.content}")


asyncio.run(main())

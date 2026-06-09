import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.retrieval import retrieve


async def main():
    r = await retrieve("nate", "What am I building and what are my constraints?")
    print(f"CORE ({len(r['core'])}):")
    for m in r["core"]:
        print(f"  [{m.category} s{m.salience}] {m.content}")
    print(f"\nTOPIC ({len(r['topic'])}):")
    for m in r["topic"]:
        print(f"  [{m.category} s{m.salience}] {m.content}")


asyncio.run(main())

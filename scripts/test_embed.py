import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.embeddings import embed, EMBED_DIM


async def main():
    vec = await embed("Nate is building Nervice, a personal AI with persistent memory.")
    print(f"dim: {len(vec)} (expected {EMBED_DIM})")
    print(f"first 5: {vec[:5]}")
    assert len(vec) == EMBED_DIM
    print("PASS: embedding works")


asyncio.run(main())

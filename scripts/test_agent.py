import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.agent import ask_claude

TASK = """A farmer has a 40-liter tank. Pipe A fills at 3 L/min. Pipe B drains at 2 L/min but only runs while the tank is above 10 L. Both start at once with the tank at 12 L. To the nearest second, when is the tank full? Show your reasoning, then give the final time."""


async def main():
    print("Asking Claude via Agent SDK (Pro account)...")
    out = await ask_claude(TASK)
    print(out)


asyncio.run(main())

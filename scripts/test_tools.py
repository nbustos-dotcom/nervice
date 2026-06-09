import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.llm import chat_with_tools
from app.tools import TOOLS, TOOL_FUNCS
from app.persona import PERSONA


async def main():
    q = "What's a major tech news story from this week? Search the web for it."
    reply = await chat_with_tools(PERSONA, [{"role": "user", "content": q}], TOOLS, TOOL_FUNCS)
    print("NERVICE:", reply)


asyncio.run(main())

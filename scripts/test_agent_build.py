import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.agent import agent_task, last_run, WORKSPACE

TASK = ("Build a small personal landing page for Nate: index.html, style.css, script.js. "
        "Dark theme, hero section with his name, a short about blurb, and one simple interactive "
        "element (theme toggle or typing effect). Real content, no lorem ipsum.")


async def main():
    print("Running builder agent...")
    out = await agent_task(TASK)
    print("\n===== AGENT SUMMARY =====")
    print(out)
    print("\n===== COST =====")
    print(f"total_cost_usd={last_run.get('cost_usd')}  num_turns={last_run.get('num_turns')}  is_error={last_run.get('is_error')}")
    print("\n===== WORKSPACE FILES =====")
    for p in sorted(WORKSPACE.rglob("*")):
        if p.is_file():
            print(f"{p.relative_to(WORKSPACE)}  {p.stat().st_size} bytes")


asyncio.run(main())

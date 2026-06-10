import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.agent import agent_task, last_run, WORKSPACE

TASK = ("In a new subfolder 'greeter', create a Python script greet.py that prints a friendly "
        "greeting with the current date, run it with python to confirm it works, then git init "
        "the workspace if needed and commit everything with a clear message. Report the script "
        "output and git log.")


async def main():
    print("Running dev-capability test...")
    out = await agent_task(TASK)
    print("\n===== AGENT SUMMARY =====")
    print(out)
    print("\n===== COST =====")
    print(f"cost_usd={last_run.get('cost_usd')}  num_turns={last_run.get('num_turns')}  is_error={last_run.get('is_error')}")
    print("\n===== WORKSPACE FILES =====")
    for p in sorted(WORKSPACE.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            print(f"{p.relative_to(WORKSPACE)}  {p.stat().st_size} bytes")
    greet = WORKSPACE / "greeter" / "greet.py"
    print(f"\ngreeter/greet.py exists: {greet.exists()}")


asyncio.run(main())

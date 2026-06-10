import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import asyncio
from app.agent import browse_agent, last_run


async def main():
    print("Running browse capability test (a visible browser will open)...")
    report = await browse_agent(
        "Go to https://news.ycombinator.com, read the top 3 story titles, open the #1 story's "
        "comments page, and report the titles plus the general sentiment of the top few comments.")
    print("\n===== AGENT REPORT =====")
    print(report)
    print("\n===== COST =====")
    print(f"cost_usd={last_run.get('cost_usd')}  turns={last_run.get('num_turns')}  is_error={last_run.get('is_error')}")


asyncio.run(main())

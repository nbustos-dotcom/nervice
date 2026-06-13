"""ONE-TIME Canvas login for Nervice's read-only browser (Phase 1 of screen control).

Launches Nervice's DEDICATED, VISIBLE browser profile so you log into Canvas BY HAND — including
any school SSO and 2FA. Nothing is automated; you type your own credentials. When you're done and
on the Canvas dashboard, press ENTER here. The session then persists in the profile directory, so
you only do this once (until the school logs you out).

Run from the repo root with the venv:
    .venv\\Scripts\\python.exe scripts\\canvas_login.py

Before running, set CANVAS_URL in app/browser.py to your school's Canvas URL
(e.g. https://<yourschool>.instructure.com) — or just navigate there in the window that opens.
"""
import asyncio
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import app.net  # noqa  (truststore: Norton TLS interception)
from app import browser
from playwright.async_api import async_playwright

PROFILE = REPO / "data" / "nervice_browser_profile"


async def main():
    PROFILE.mkdir(parents=True, exist_ok=True)
    start_url = browser.CANVAS_URL or "about:blank"
    if not browser.CANVAS_URL:
        print("NOTE: CANVAS_URL is blank in app/browser.py — opening a blank page. Either set it,\n"
              "or just type your Canvas address into the window that opens.\n")
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE), headless=False, args=["--no-first-run", "--no-default-browser-check"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        print(f"Nervice's browser is open (profile: {PROFILE}).\n")
        print("  1) Log into Canvas by hand — username, password, SSO, 2FA, all manual.")
        print("  2) Get all the way to your Canvas DASHBOARD so the session is fully established.")
        print("  3) Come back here and press ENTER to save the session and close.\n")
        try:
            input("Press ENTER once you're logged in and on the dashboard... ")
        except (EOFError, KeyboardInterrupt):
            pass
        await ctx.close()
    print("\nSaved. Nervice can now read your Canvas — try asking it \"what's due this week\".")


if __name__ == "__main__":
    asyncio.run(main())

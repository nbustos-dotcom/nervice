"""ONE-TIME Canvas login for Nervice's read-only browser (Phase 1 of screen control).

Launches a fresh VISIBLE browser so you log into Canvas BY HAND — username, password, SSO, 2FA, all
manual, nothing automated. When you're on the Canvas dashboard, press ENTER here. Your login is
saved to data/nervice_browser_auth.json via Playwright storage_state — which captures the SESSION
cookies (MTU's CAS TGC, Canvas canvas_session) that a persistent profile dir does NOT restore on a
fresh launch. Nervice's reader re-injects that saved login before each Canvas read, so the session
actually carries. You only redo this when the school's session genuinely expires.

Run from the repo root with the venv:
    .venv\\Scripts\\python.exe scripts\\canvas_login.py

Set CANVAS_URL in app/browser.py first (e.g. https://mtu.instructure.com).
"""
import asyncio
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import app.net  # noqa  (truststore: Norton TLS interception)
from app import browser
from playwright.async_api import async_playwright


async def main():
    auth = browser._AUTH_STATE
    auth.parent.mkdir(parents=True, exist_ok=True)
    start_url = browser.CANVAS_URL or "about:blank"
    if not browser.CANVAS_URL:
        print("NOTE: CANVAS_URL is blank in app/browser.py — set it first, or just type your Canvas "
              "address into the window that opens.\n")
    async with async_playwright() as p:
        # A FRESH browser (NOT the persistent reader profile) so this works even while Nervice's
        # reader browser is open — no profile-dir lock fight. Pre-load any existing saved login so
        # Duo's remembered-device cookie carries and you don't redo 2FA on a refresh.
        b = await p.chromium.launch(headless=False, args=["--no-first-run", "--no-default-browser-check"])
        ctx = await b.new_context(storage_state=str(auth) if auth.exists() else None)
        page = await ctx.new_page()
        try:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        print(f"A browser is open. Your login will be saved to:\n  {auth}\n")
        print("  1) Log into Canvas by hand — username, password, SSO, 2FA, all manual.")
        print("  2) Get all the way to your Canvas DASHBOARD so the session is fully established.")
        print("  3) Come back here and press ENTER to save the session and close.\n")
        try:
            input("Press ENTER once you're logged in and on the dashboard... ")
        except (EOFError, KeyboardInterrupt):
            pass
        await ctx.storage_state(path=str(auth))   # capture cookies (incl SESSION cookies) while live
        await b.close()
    print(f"\nSaved your login to {auth.name}. Ask Nervice \"what's due this week\" to test the read.")


if __name__ == "__main__":
    asyncio.run(main())

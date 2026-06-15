# Running Nervice's server

**Auto-start on login was dropped.** It looped at real login: a Task Scheduler task and the Tauri app
both started a server, fought over `127.0.0.1:8765`, and the loser kept respawning instead of
settling. Rather than babysit two competing starters, the server is started **once, deliberately**.

## Start it
- **Manually:** `python run_api.py` from the repo root (or `.venv\Scripts\python.exe run_api.py`).
- **Or via the Tauri desktop app**, which spawns the server itself.

Either way, `run_api.py`'s port preflight makes a second start safe: if `127.0.0.1:8765` is already
serving, it prints *"Nervice is already running …"* and **exits cleanly (rc 0) — one shot, no retry,
no loop.** (It checks the port *before* importing uvicorn, so that exit is fast.)

## Kept from the autostart work (worth keeping however it's launched)
`run_api.py` is hardened so any launch is robust:
- **No-console safe** — if launched without a console (stdout/stderr are `None`), it reopens them to
  `logs/server.out.log` before printing, so a print can't crash it.
- **cwd-independent** — it `chdir`s to the repo and loads `.env` by absolute path, so the launch
  directory doesn't matter.
- **Clean exit on a port-bind race** — if two starts race past the preflight, the loser exits cleanly
  instead of dumping a WinError 10048 traceback.

## Future
A "Hey Nervice" wake-word trigger may start the server on demand instead of at login — a single,
intentional starter, with no login-time contention.

> No scheduled task is created by anything in this repo. The old `scripts/start_nervice.vbs` launcher
> was removed and stays removed. If you previously created the Task Scheduler task, delete it:
> `schtasks /Delete /TN "Nervice Server" /F`

# Running Nervice's server

Auto-start on login was **removed** — it caused a relaunch loop (a scheduled task kept respawning a
server that couldn't bind the already-held port). Start the server yourself:

- **Manually:** `python run_api.py` from the repo root (or `.venv\Scripts\python.exe run_api.py`).
- **Or via the Tauri desktop app**, which spawns the server itself.

A future "Hey Nervice" wake-word trigger may start the server on demand instead.

A second start is harmless: `run_api.py`'s port preflight sees `127.0.0.1:8765` already serving,
prints one honest line, and **exits cleanly (rc 0) — one shot, no retry, no loop.**

> Nothing in this repo creates a scheduled task or a Startup-folder entry, and the old
> `scripts/start_nervice.vbs` launcher is removed. If you still have the old Task Scheduler task,
> delete it: `schtasks /Delete /TN "Nervice Server" /F`

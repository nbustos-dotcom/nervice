# Nervice auto-start on login (Windows)

Goal: the server is already running when you reach for it — no more "open a terminal and start it"
(and no more unknowingly using a stale server). Free, native, no new dependencies.

**Recommended: a Task Scheduler task that runs `scripts/start_nervice.vbs` at log on.** The `.vbs`
launches `python run_api.py` **hidden and detached** (no console window, survives the launcher
exiting, forrtl-safe). It coexists with the Tauri app: `run_api.py`'s port preflight means whichever
starts first wins `127.0.0.1:8765` and the other exits cleanly — so this never double-binds or breaks
the Tauri spawn.

> These steps are for **you to run** — nothing here is applied automatically.

## Option A — one command (fastest)

Run once in a normal (non-admin) terminal:

```
schtasks /Create /TN "Nervice Server" /SC ONLOGON /F ^
  /TR "wscript.exe \"C:\Users\nateb\nervice\scripts\start_nervice.vbs\""
```

That's it — it now starts at every log on. Test it immediately without rebooting:

```
schtasks /Run /TN "Nervice Server"
```

Then check it came up (PowerShell):

```
Invoke-RestMethod http://127.0.0.1:8765/health -Headers @{ Authorization = "Bearer $env:NERVICE_API_TOKEN" }
```

(`status: ok` and a `git` hash = it's up. `stale: true` would mean the running server predates your
latest commit — restart it.)

To remove it later: `schtasks /Delete /TN "Nervice Server" /F`

## Option B — Task Scheduler GUI (same thing, clickable)

1. Open **Task Scheduler** → **Create Basic Task…**
2. Name: `Nervice Server`. Trigger: **When I log on**.
3. Action: **Start a program**.
   - Program/script: `wscript.exe`
   - Add arguments: `"C:\Users\nateb\nervice\scripts\start_nervice.vbs"`
4. Finish. (Optional: in the task's properties, untick "Stop the task if it runs longer than…" so a
   long-lived server isn't killed.)

## Notes

- **Restart after a code change.** The server runs the code it booted with. After `git pull`/commit,
  restart it (the HUD now shows **SERVER STALE — restart to load new code** when the running server's
  hash != your latest commit). Quickest restart: `schtasks /Run /TN "Nervice Server"` won't help if one
  is already bound — stop the old one first (close it / kill the `python.exe` on :8765), then run the
  task again, or just let the next login restart it.
- **Logs:** the hidden server still writes `logs/turns.log`, `logs/server.log` (boot lines with the
  git hash + launcher), and the action/audit logs — so you can confirm what's running without a window.
- **Tauri app:** if you usually open the desktop app (which spawns its own server), you may not need
  this task at all. Use this when you want the server up **without** opening the app.

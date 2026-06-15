# Nervice auto-start on login (Windows)

Goal: the server is already running when you reach for it — no terminal, no stale server.

**Mechanism: a Task Scheduler "At log on" task that runs `pythonw.exe run_api.py` directly, 30s after
logon.** No wrapper script, **no Windows Script Host**.

> **Why this replaced the old VBS/wscript launcher.** The previous `scripts/start_nervice.vbs` was run
> via `wscript.exe`, and Windows Script Host is unreliable at login — it failed with *"Execution of the
> Windows Script Host failed. Not enough memory resources"* during the early-login resource crunch.
> `pythonw.exe` is a plain GUI-subsystem Python (no console window, no WSH), so that whole failure class
> is gone. It's also forrtl-safe: with no console there is no window-close event for MKL to trip on.
> `run_api.py` reopens its (null) stdout/stderr to `logs/server.out.log`, `chdir`s to the repo, and
> loads `.env` by absolute path, so it runs correctly even though Task Scheduler launches it from
> `System32` with no console. The `/DELAY 0000:30` lets the login storm settle (the "not enough memory"
> cause). `run_api.py`'s port preflight still makes it coexist with a manually- or Tauri-started
> server — a second instance prints "already running" and exits cleanly.

## Swap the scheduled task (run these once — nothing here is applied for you)

**1. Delete the old (broken, WSH) task:**
```
schtasks /Delete /TN "Nervice Server" /F
```

**2. Create the new (pythonw, 30s delay) task:**
```
schtasks /Create /TN "Nervice Server" /SC ONLOGON /DELAY 0000:30 /F /TR "C:\Users\nateb\nervice\.venv\Scripts\pythonw.exe C:\Users\nateb\nervice\run_api.py"
```
(The paths have no spaces, so no inner quoting is needed. `/DELAY mmmm:ss` is valid for `ONLOGON`.)

**3. Test it now, without rebooting:**
```
schtasks /Run /TN "Nervice Server"
```
Wait a few seconds, then confirm it came up (PowerShell):
```
Invoke-RestMethod http://127.0.0.1:8765/health -Headers @{ Authorization = "Bearer $env:NERVICE_API_TOKEN" }
```
`status: ok` + a `git` hash = it's up. `stale: true` means the running server predates your latest
commit — restart it. There should be **no console window** and **no WSH error**.

**4. Then test the real path: reboot / log out and back in.** The server should be answering on
`http://127.0.0.1:8765` ~30s after you reach the desktop.

To remove later: `schtasks /Delete /TN "Nervice Server" /F`.

## Notes
- **Optional resilience:** in the task's Properties → Settings, tick *"If the task fails, restart every
  1 minute, up to 3 times"* — belt-and-suspenders if a login is ever still resource-starved at +30s.
  You can also raise the delay (e.g. `/DELAY 0001:00` for 60s).
- **Logs:** the hidden server writes `logs/server.out.log` (stdout/stderr — boot lines, errors),
  `logs/server.log` (one line per boot: time, git hash, launcher), and the usual `logs/turns.log` /
  audit logs. So you can confirm what's running without a window.
- **Restart after a code change:** the server runs the code it booted with; the HUD shows
  **SERVER STALE — restart to load new code** when the running hash != your latest commit. Stop the
  old one (kill the `pythonw.exe` on :8765, or it's replaced at next login) and re-run the task.
- **Tauri app:** if you usually open the desktop app (it spawns its own server), you may not need this
  task; the port preflight keeps them from colliding. Use the task to have the server up *without*
  opening the app.
- The old `scripts/start_nervice.vbs` has been removed — do not recreate the wscript approach.

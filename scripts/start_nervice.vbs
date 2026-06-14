' Nervice auto-start launcher — hidden + detached.
'
' Runs the API server with NO visible window and lets this script exit immediately while the server
' keeps running. Intended to be invoked by a Windows Task Scheduler "At log on" task so the server is
' already up when Nate reaches for it (see docs/AUTOSTART.md).
'
' Why this shape:
'  - python.exe (NOT pythonw) keeps a real-but-hidden console, so the server's stdout/stderr stay
'    valid (prints/logs work) and the MKL "forrtl" window-close crash can't fire — the console is
'    never force-closed; the python process owns it.
'  - windowStyle 0 = hidden (no flashing console). bWaitOnReturn False = detached (survives this exit).
'  - run_api.py's own port-in-use preflight means if the Tauri desktop app already spawned a server,
'    this one prints "already running" and exits cleanly — no conflict, nothing double-bound.
'
' Working dir is set to the repo root (resolved from this script's location) so run_api.py finds
' .env and the git repo.

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
repoRoot  = fso.GetParentFolderName(scriptDir)

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = repoRoot
sh.Run """" & repoRoot & "\.venv\Scripts\python.exe"" run_api.py", 0, False

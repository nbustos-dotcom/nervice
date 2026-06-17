@echo off
REM ============================================================================
REM  Nervice screen-control OUT-OF-BAND PANIC STOP.
REM  Double-click this (or bind it to a Stream Deck / AutoHotkey / taskbar key) to
REM  trip the SAME freeze the Phase-1 policy gate reads -- no server, no HUD, no
REM  token required. It writes the sentinel data\screen_freeze.flag via the exact
REM  same set_freeze() the API uses, so check_can_act() then returns DENY("frozen").
REM  It only ever SETS the freeze; re-arming is a deliberate action in the HUD
REM  (or POST /screen/unfreeze) -- never here.
REM  To bind a hotkey: right-click this file > Create shortcut, then open the
REM  shortcut's Properties > Shortcut key (e.g. Ctrl+Alt+F12) -- Windows fires it system-wide.
REM  (Even barer fallback if the venv were ever broken:  type nul >> data\screen_freeze.flag)
REM ============================================================================
cd /d "%~dp0.."
".venv\Scripts\python.exe" -c "from app import screen_policy; screen_policy.set_freeze('freeze.bat out-of-band panic')"
echo.
echo   SCREEN CONTROL FROZEN. Re-arm from the HUD (or POST /screen/unfreeze) when ready.
echo.

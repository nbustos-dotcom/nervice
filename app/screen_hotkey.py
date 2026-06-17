"""Global OS PANIC HOTKEY for the screen-control freeze.

Pressing Ctrl+Alt+Shift+F12 anywhere on the machine trips the SAME freeze the Phase-1 policy gate
already reads: it calls screen_policy.set_freeze(), which writes data/screen_freeze.flag, which makes
screen_policy.check_can_act() return DENY("frozen"). One source of truth, three+ panic paths (HUD
button, REST endpoint, this hotkey, a bare touch of the file).

Implementation: pure ctypes RegisterHotKey + a Win32 message loop on a daemon thread. NO new
dependency — `keyboard` is deliberately NOT used (it isn't installed, and adding it would need a
flagged install). pywin32 is present but ctypes keeps this dependency-free and self-contained.

GRACEFUL DEGRADE (the whole point): if the hotkey cannot register — chord already taken, a
console-less / session-0 host with no message queue, or any error — it LOGS and returns. The HUD
button, POST /screen/freeze, and touching the sentinel file all still work. The hotkey is a
convenience, never the only stop.

Scope: this is a FREEZE control, not an acting primitive. It can ONLY set the freeze; clearing is an
explicit human re-arm elsewhere (POST /screen/unfreeze / the HUD). It never imports a browser or any
acting code.
"""
import sys
import ctypes
import ctypes.wintypes
import threading

from app import screen_policy

# Win32 constants (kept local — pure ctypes, no win32con import needed).
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000          # ignore key auto-repeat while held (Win8+)
_VK_F12 = 0x7B
_WM_HOTKEY = 0x0312
_HOTKEY_ID = 0xB10C             # arbitrary id, unique within this thread's message queue

CHORD = "Ctrl+Alt+Shift+F12"
_state = {"status": "not-started"}


def _loop() -> None:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except Exception as e:
        _state["status"] = f"degraded: no user32 ({repr(e)[:50]})"
        print(f"[screen_hotkey] {_state['status']} — HUD button + file panic still work", file=sys.stderr)
        return

    mods = _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT
    if not user32.RegisterHotKey(None, _HOTKEY_ID, mods, _VK_F12):
        err = ctypes.get_last_error()
        _state["status"] = f"degraded: RegisterHotKey failed (err={err})"
        print(f"[screen_hotkey] could not register {CHORD} (err={err}) — "
              "HUD button + REST + file panic still work", file=sys.stderr)
        return

    _state["status"] = f"armed: {CHORD}"
    print(f"[screen_hotkey] global panic hotkey ARMED: {CHORD}", file=sys.stderr)

    msg = ctypes.wintypes.MSG()
    try:
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):          # WM_QUIT or error -> exit the loop
                break
            if msg.message == _WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                try:
                    screen_policy.set_freeze(f"global hotkey {CHORD}")
                    print("[screen_hotkey] PANIC HOTKEY -> freeze set", file=sys.stderr)
                except Exception as e:
                    print(f"[screen_hotkey] set_freeze failed: {repr(e)[:80]}", file=sys.stderr)
    except Exception as e:
        _state["status"] = f"degraded: loop error ({repr(e)[:50]})"
        print(f"[screen_hotkey] message loop error: {repr(e)[:80]}", file=sys.stderr)
    finally:
        try:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
        except Exception:
            pass


def start() -> str:
    """Start the listener in a daemon thread. Returns immediately; NEVER raises (a panic stop must
    not be able to crash server startup). The registration result lands in status() a moment later."""
    if not sys.platform.startswith("win"):
        _state["status"] = "degraded: not Windows"
        return _state["status"]
    try:
        threading.Thread(target=_loop, name="screen-hotkey", daemon=True).start()
        if _state["status"] == "not-started":
            _state["status"] = "starting"
    except Exception as e:
        _state["status"] = f"degraded: thread start failed ({repr(e)[:50]})"
        print(f"[screen_hotkey] {_state['status']}", file=sys.stderr)
    return _state["status"]


def status() -> str:
    return _state["status"]

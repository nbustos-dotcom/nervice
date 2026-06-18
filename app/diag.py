"""Read-only, NON-BILLABLE diagnostics.

claude_spawn_selftest() runs the Agent-SDK's CLI-spawn mechanism with `--version` ONLY — never a
query, no prompt, no tokens, ZERO spend. Its purpose: from the REAL Tauri-launched (console-less)
server, check whether the spawn-time WinError 50 still reproduces. The bash-launched server can't
recreate the Tauri GUI-process/session context, so this lets Nate hit the exact spawn path on the
real launcher and read the result.

It deliberately mirrors what the SDK does internally (see
.venv/.../claude_agent_sdk/_internal/transport/subprocess_cli.py):
  - resolves the CLI the SDK's `_find_cli` way: bundled `_bundled/claude.exe` first, else PATH (:81-121)
  - spawns via `anyio.open_process([cli, "--version"], stdin/stdout/stderr=...)` (:474), the exact
    failing call, trying BOTH stderr=None (the SDK's production default: inherit the parent handle)
    and stderr=PIPE.
It NEVER imports or calls the SDK's `query()` (the billable path) and NEVER touches the spend guard,
the account ladder, or any credential. It catches OSError/WinError and reports it as data.
"""
import sys
import platform
from pathlib import Path


def _resolve_cli() -> tuple[str | None, str | None]:
    """Mirror the SDK's _find_cli(): the bundled claude.exe first, then PATH. Returns (path, how)
    or (None, None). Pure filesystem lookup — no launching."""
    try:
        import claude_agent_sdk
        name = "claude.exe" if platform.system() == "Windows" else "claude"
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        if bundled.exists():
            return str(bundled), "bundled"
    except Exception:
        pass
    try:
        import shutil
        on_path = shutil.which("claude")
        if on_path:
            return on_path, "PATH"
    except Exception:
        pass
    return None, None


async def _spawn_version(cli: str, stderr_inherit: bool) -> dict:
    """One run of the EXACT SDK spawn mechanism (anyio.open_process) with `--version`. NON-BILLABLE.
    stderr_inherit=True == the SDK's production default (stderr=None -> inherit the parent handle, the
    variant that raised WinError 50 console-less); False == stderr=PIPE. NEVER raises: an OSError /
    [WinError 50] is caught and returned as {ok:False, winerror, error_type, error_msg}."""
    from subprocess import PIPE
    import anyio
    kw = dict(stdin=PIPE, stdout=PIPE)
    kw["stderr"] = None if stderr_inherit else PIPE        # None mirrors subprocess_cli.py:472 default
    try:
        proc = await anyio.open_process([cli, "--version"], **kw)
        out = b""
        if proc.stdout:
            try:
                out = await proc.stdout.receive()
            except Exception:
                pass
        await proc.wait()
        return {"ok": True, "rc": proc.returncode,
                "stdout": out.decode("utf-8", "replace").strip()[:60],
                "winerror": None, "error_type": None, "error_msg": None}
    except BaseException as e:
        return {"ok": False, "rc": None, "stdout": None,
                "winerror": getattr(e, "winerror", None),
                "error_type": type(e).__name__, "error_msg": repr(e)[:200]}


async def claude_spawn_selftest() -> dict:
    """NON-BILLABLE self-test: spawn the bundled claude.exe `--version` via the SDK's anyio.open_process
    mechanism, both stderr variants. No query, no prompt, no tokens, no state change. Returns a
    structured result so GET /diag/claude-spawn (and Nate, from the real Tauri server) can see whether
    the console-less WinError 50 spawn reproduces."""
    cli, how = _resolve_cli()
    info = {
        "billable": False,
        "python": sys.version.split()[0],
        "launched_via": Path(sys.executable).name,
        "cli_resolved_via": how,
        "bundled_cli": cli,
    }
    try:
        if sys.platform == "win32":
            import ctypes
            info["console_window"] = bool(ctypes.windll.kernel32.GetConsoleWindow())
        else:
            info["console_window"] = None
    except Exception:
        info["console_window"] = None

    if cli is None:
        info["error"] = "claude CLI not found (bundled or on PATH) — is claude_agent_sdk installed?"
        info["reproduces_winerror50"] = None
        return info

    info["stderr_inherit_None_PRODUCTION"] = await _spawn_version(cli, stderr_inherit=True)
    info["stderr_PIPE"] = await _spawn_version(cli, stderr_inherit=False)
    info["reproduces_winerror50"] = any(
        (info.get(k) or {}).get("winerror") == 50
        for k in ("stderr_inherit_None_PRODUCTION", "stderr_PIPE"))
    return info

import os
import sys
from dotenv import load_dotenv

# The server prints replies to the console for debugging, and a reply can contain Unicode the
# replies use (em-dashes, arrows, a warning glyph). Windows' default cp1252 stdout raises
# UnicodeEncodeError on an unencodable char, which 500s the whole turn. utf-8 + replace can never
# crash — worst case a stray glyph becomes "?". Boot lines stay ASCII either way (see below).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv()
import uvicorn

# Bound to localhost for now. Phase B will bind to the Tailscale tailnet interface — NEVER expose
# this publicly (no port-forwarding); reach it from the phone over Tailscale's private network.
HOST, PORT = "127.0.0.1", 8765


def _git_hash():
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _port_in_use(host, port):
    """True if something already accepts connections on host:port. Pure probe — no bind attempt,
    so it can't itself trip the WinError 10048 it exists to prevent."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def _identify_occupant(host, port, token):
    """The port is held — find out by WHAT. Asks /health with our own token.
    Returns ("nervice", version|None) when the occupant is a Nervice server, else ("other", None).
    A 401/403 on /health is Nervice's auth gate answering, so that counts as Nervice too."""
    import json
    import urllib.error
    import urllib.request
    url = f"http://{host}:{port}/health"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2) as r:
            body = r.read(4096).decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except Exception:
            return ("other", None)
        if isinstance(data, dict) and data.get("status") == "ok":
            return ("nervice", data.get("git"))
        return ("other", None)
    except urllib.error.HTTPError as e:
        return ("nervice", None) if e.code in (401, 403) else ("other", None)
    except Exception:
        # Something holds the port but gives no clean /health answer — not a Nervice server
        # (or not HTTP at all). Treat as a foreign occupant.
        return ("other", None)


def _write_boot_log(git_hash, port):
    """One line per real boot: when, which commit, who launched it. Lets a later session prove the
    live server matches HEAD (the recurring stale-server trap). A failed write never blocks boot."""
    import datetime
    import pathlib
    launcher = "spawned by tauri" if os.environ.get("NERVICE_LAUNCHER") == "tauri" else "manual"
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    try:
        logs = pathlib.Path(__file__).resolve().parent / "logs"
        logs.mkdir(exist_ok=True)
        with open(logs / "server.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {git_hash} {launcher} :{port}\n")
    except Exception:
        pass


if __name__ == "__main__":
    if not os.environ.get("NERVICE_API_TOKEN"):
        raise SystemExit("NERVICE_API_TOKEN is not set in .env — generate one with "
                         "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")

    _hash = _git_hash()

    # Preflight: a held port used to be a fatal WinError 10048 stack trace with no guidance. Now we
    # look first and exit 0 with a plain-English line — never a traceback the owner has to decode.
    if _port_in_use(HOST, PORT):
        kind, version = _identify_occupant(HOST, PORT, os.environ.get("NERVICE_API_TOKEN"))
        if kind == "nervice":
            ver = f" (version {version})" if version else ""
            print(f"Nervice is already running{ver} on {HOST}:{PORT} - open the app, or quit the "
                  f"running one first if you want a fresh server.")
        else:
            print(f"Port {PORT} is held by another program (not Nervice). Close it, or free "
                  f"{HOST}:{PORT}, then start Nervice again.")
        raise SystemExit(0)

    _write_boot_log(_hash, PORT)

    import datetime
    print(f"Nervice API -> http://{HOST}:{PORT}")   # ASCII: survives cp1252 redirected consoles
    print(f"version {_hash} · booted {datetime.datetime.now().isoformat(timespec='seconds')}")
    print("Every endpoint requires header:  Authorization: Bearer $NERVICE_API_TOKEN")
    print("(localhost only for now; Tailscale tailnet binding comes in Phase B — never public.)")
    uvicorn.run("app.api:app", host=HOST, port=PORT)

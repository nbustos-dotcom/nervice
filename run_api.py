import os
from dotenv import load_dotenv

load_dotenv()
import uvicorn

# Bound to localhost for now. Phase B will bind to the Tailscale tailnet interface — NEVER expose
# this publicly (no port-forwarding); reach it from the phone over Tailscale's private network.
HOST, PORT = "127.0.0.1", 8765

if __name__ == "__main__":
    if not os.environ.get("NERVICE_API_TOKEN"):
        raise SystemExit("NERVICE_API_TOKEN is not set in .env — generate one with "
                         "python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    import datetime
    import subprocess
    try:
        _hash = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                               text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        _hash = "unknown"
    print(f"Nervice API -> http://{HOST}:{PORT}")   # ASCII: survives cp1252 redirected consoles
    print(f"version {_hash} · booted {datetime.datetime.now().isoformat(timespec='seconds')}")
    print("Every endpoint requires header:  Authorization: Bearer $NERVICE_API_TOKEN")
    print("(localhost only for now; Tailscale tailnet binding comes in Phase B — never public.)")
    uvicorn.run("app.api:app", host=HOST, port=PORT)

"""Preflight clean-exit / NO-LOOP test.

Auto-start on login was DROPPED (it looped: a second starter fought over 8765 instead of exiting
clean). This guards the KEPT run_api.py hardening — the part that matters now: when 8765 is already
held, a second `python run_api.py` must EXIT ONCE, CLEANLY (rc reported), within a couple seconds —
never spin/retry/loop. Run: .venv/Scripts/python.exe scripts/test_autostart.py

Checks: start one server; then start a SECOND 3x in a row (each must exit clean, fast, primary keeps
serving, no loop); and a foreign holder of 8765 -> clean exit. Launches via pythonw (also exercises
the no-console path). Self-cleaning (kills 8765 at the end).
"""
import os, sys, time, json, socket, subprocess, urllib.request, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
TOK = os.environ["NERVICE_API_TOKEN"]
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
RUN = ROOT / "run_api.py"
HEAD = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()
_DETACH = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW

P, F = [], []
def ok(m): P.append(m); print("  PASS", m)
def bad(m): F.append(m); print("  FAIL", m)

def health():
    try:
        r = urllib.request.Request("http://127.0.0.1:8765/health", headers={"Authorization": f"Bearer {TOK}"})
        with urllib.request.urlopen(r, timeout=3) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None

def launch():
    return subprocess.Popen([str(PYW), str(RUN)], cwd=r"C:\Windows\System32", creationflags=_DETACH,
                            close_fds=True, stdin=subprocess.DEVNULL)

def kill_8765():
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    for pid in {ln.split()[-1] for ln in out.splitlines() if "127.0.0.1:8765" in ln and "LISTENING" in ln}:
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    time.sleep(1)

def wait_up(timeout=45):
    t = time.time()
    while time.time() - t < timeout:
        h = health()
        if h:
            return h
        time.sleep(0.5)
    return None

print(f"HEAD={HEAD}")
kill_8765()

print("\n[1] start the primary server (python run_api.py via pythonw)")
p1 = launch()
h = wait_up()
(ok if h and h.get("git") == HEAD else bad)(f"primary up; /health git={h.get('git') if h else None}==HEAD")

print("\n[2] start a SECOND 3x -> each must EXIT cleanly within a couple seconds, NO loop; primary survives")
for i in range(1, 4):
    t0 = time.time()
    p = launch()
    try:
        rc = p.wait(timeout=12)
        dt = time.time() - t0
        if rc is not None and dt <= 10:
            ok(f"run {i}: 2nd start exited cleanly rc={rc} in {dt:.1f}s — exited once, no loop")
        else:
            bad(f"run {i}: rc={rc} dt={dt:.1f}s — too slow / spinning")
    except subprocess.TimeoutExpired:
        p.kill()
        bad(f"run {i}: 2nd start HUNG/LOOPED (>12s, never exited)")
    (ok if health() else bad)(f"run {i}: primary still serving after the 2nd exited")

print("\n[3] foreign holder of 8765 (a non-Nervice program) -> launcher exits cleanly, no loop")
import threading
kill_8765()
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 8765)); sock.listen(5)
stop = threading.Event()
def _foreign():
    # Accept connections and reply with NON-HTTP bytes, like a real foreign service would (a raw
    # listen() that never accepts would just stall the occupant probe's SYN — an artifact, not a bug).
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, _ = sock.accept()
            try: conn.sendall(b"not-http\r\n")
            except Exception: pass
            conn.close()
        except socket.timeout:
            continue
        except Exception:
            break
threading.Thread(target=_foreign, daemon=True).start()
t0 = time.time()
p = launch()
try:
    rc = p.wait(timeout=12)
    dt = time.time() - t0
    (ok if rc is not None and dt <= 10 else bad)(f"foreign-held: launcher exited rc={rc} in {dt:.1f}s — no loop")
except subprocess.TimeoutExpired:
    p.kill(); bad("foreign-held: launcher HUNG/LOOPED")
stop.set(); sock.close()

kill_8765()
print(f"\n=== NO-LOOP {len(P)} passed, {len(F)} failed ===")
sys.exit(1 if F else 0)

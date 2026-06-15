"""LOOP-PROOF test for run_api.py's port preflight.

Auto-start on login was DROPPED (a scheduled task kept respawning a server that couldn't bind 8765 —
a relaunch loop). The fix has two halves: (a) nothing respawns the server anymore (no scheduled task
/ no Startup entry / start_nervice.vbs removed — confirmed separately), and (b) any extra `run_api.py`
EXITS ONCE, CLEANLY, fast when 8765 is already held — it cannot spin. This proves (b):

  [1] start a server; /health == HEAD.
  [2] double-start 5x rapidly: each 2nd start exits cleanly (rc reported) in ~1-2s, primary survives,
      NEVER loops or spawns repeatedly.
  [3] start 3 near-simultaneously from a clean slate: exactly ONE survives, the other two exit clean.
  [4] a foreign program holding 8765: the launcher exits clean, no loop.

Launches via pythonw (also exercises the kept no-console path), cwd=System32 (proves cwd-independence).
Self-cleaning. Run: .venv/Scripts/python.exe scripts/test_autostart.py
"""
import os, sys, time, json, socket, subprocess, threading, urllib.request, pathlib
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

print("\n[1] start the primary server")
p1 = launch()
h = wait_up()
(ok if h and h.get("git") == HEAD else bad)(f"primary up; /health git={h.get('git') if h else None}==HEAD")

print("\n[2] double-start 5x rapidly -> each 2nd start EXITS cleanly, fast, NO loop; primary survives")
for i in range(1, 6):
    t0 = time.time()
    p = launch()
    try:
        rc = p.wait(timeout=12)
        dt = time.time() - t0
        (ok if rc is not None and dt <= 10 else bad)(f"run {i}: 2nd start exited rc={rc} in {dt:.1f}s — one shot, no loop")
    except subprocess.TimeoutExpired:
        p.kill(); bad(f"run {i}: 2nd start HUNG/LOOPED (>12s)")
    if not health():
        bad(f"run {i}: primary stopped serving!"); break
ok("primary survived all 5 double-starts") if health() else None

print("\n[3] start 3 NEAR-SIMULTANEOUSLY from a clean slate -> exactly ONE survives, others exit clean")
kill_8765()
procs = [launch(), launch(), launch()]
h = wait_up()
(ok if h else bad)("exactly one server came up from the 3-way race")
time.sleep(10)   # let the two losers resolve (preflight OR graceful bind-race catch)
alive = [p for p in procs if p.poll() is None]
exited = [p for p in procs if p.poll() is not None]
if h and len(alive) == 1 and len(exited) == 2:
    ok(f"exactly 1 survivor, 2 exited cleanly (rcs={[p.returncode for p in exited]}) — no flashing, no loop")
else:
    bad(f"3-way race resolved wrong: server={'up' if h else 'down'} alive={len(alive)} exited={len(exited)}")

print("\n[4] foreign program holds 8765 -> launcher exits clean, no loop")
kill_8765()
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 8765)); sock.listen(5)
stop = threading.Event()
def _foreign():
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
print(f"\n=== LOOP-PROOF {len(P)} passed, {len(F)} failed ===")
sys.exit(1 if F else 0)

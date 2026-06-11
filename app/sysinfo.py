"""Read-only machine + self awareness. The /system HUD endpoint and the conversational tools both
read from here (no duplication). STRICTLY read-only: this inspects specs / live stats / process
lists / file COUNTS — it never modifies, deletes, kills, runs a shell, or reads file contents.
Acting on the machine (the screen-control boundary) is a separate phase and lives in app/computer.py.
"""
import os
import re
import time
import platform
import pathlib
import subprocess

import psutil

# ----------------------------- live telemetry (shared with the /system endpoint) -----------------------------
_net_prev = {"t": None, "sent": 0, "recv": 0}


def gpu_telemetry() -> dict:
    """GPU stats via nvidia-smi (CSV) incl. the real model name. {available:false} if the tool/GPU
    isn't reachable — no fabricated numbers and no hardcoded model when there's no GPU."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3).stdout.strip().splitlines()[0]
        parts = [x.strip() for x in out.split(",")]
        u, vu, vt, tp = parts[:4]
        name = ",".join(parts[4:]).strip() or "GPU"      # real driver-reported model string
        return {"available": True, "util": int(u), "vram_used": int(vu), "vram_total": int(vt),
                "temp": int(tp), "name": name}
    except Exception:
        return {"available": False}


def system_telemetry() -> dict:
    """Synchronous psutil snapshot (run in a thread). Per-core CPU over a short real sample; RAM,
    swap, disk for C:, network up/down RATES (delta since last call), uptime, process count."""
    per = psutil.cpu_percent(interval=0.15, percpu=True)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    du = psutil.disk_usage("C:\\")
    n = psutil.net_io_counters()
    now = time.time()
    primed = _net_prev["t"] is not None          # first poll has no baseline -> report null, not a fake 0
    up_bps = down_bps = None
    if primed:
        dt = max(0.001, now - _net_prev["t"])
        up_bps = round(max(0, n.bytes_sent - _net_prev["sent"]) / dt)
        down_bps = round(max(0, n.bytes_recv - _net_prev["recv"]) / dt)
    _net_prev.update(t=now, sent=n.bytes_sent, recv=n.bytes_recv)
    return {
        "cpu": {"total": round(sum(per) / len(per), 1), "cores": [round(x, 1) for x in per], "count": len(per)},
        "ram": {"percent": vm.percent, "used": vm.used, "total": vm.total},
        "swap": {"percent": sw.percent},
        "disk": {"percent": du.percent, "used": du.used, "total": du.total},
        "net": {"up_bps": up_bps, "down_bps": down_bps},
        "uptime_s": int(now - psutil.boot_time()),
        "processes": len(psutil.pids()),
        "gpu": gpu_telemetry(),
    }


# ----------------------------- conversational tools (LLM-facing, human-readable) -----------------------------
_cpu_name_cache = None


def _cpu_name() -> str:
    global _cpu_name_cache
    if _cpu_name_cache:
        return _cpu_name_cache
    name = None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except Exception:
        pass
    _cpu_name_cache = name or (platform.processor() or "Unknown CPU")
    return _cpu_name_cache


def get_system_info() -> str:
    """One-line-per-fact summary of THIS machine: CPU model + cores + live %, RAM, GPU, disk, uptime,
    OS. Read-only."""
    t = system_telemetry()
    g = t["gpu"]
    up = t["uptime_s"]; d, h, m = up // 86400, (up % 86400) // 3600, (up % 3600) // 60
    parts = [
        f"CPU: {_cpu_name()} — {t['cpu']['count']} logical cores, {t['cpu']['total']:.0f}% in use now.",
        f"RAM: {t['ram']['used']/1e9:.1f} of {t['ram']['total']/1e9:.1f} GB used ({t['ram']['percent']:.0f}%).",
        (f"GPU: {g['name']} — {g['util']}% util, {g['vram_used']}/{g['vram_total']} MB VRAM, {g['temp']}°C."
         if g.get("available") else "GPU: none detected via nvidia-smi."),
        f"Disk C:: {t['disk']['used']/1e9:.0f} of {t['disk']['total']/1e9:.0f} GB used ({t['disk']['percent']:.0f}%).",
        (f"Uptime: {d}d {h}h." if d else f"Uptime: {h}h {m}m."),
        f"OS: {platform.platform()}. Processes running: {t['processes']}.",
    ]
    return " ".join(parts)


def get_top_processes(by: str = "memory", n: int = 5) -> str:
    """Real running processes sorted by memory (RSS) or CPU. Read-only — only reads process stats,
    never kills or signals anything."""
    by = (by or "memory").lower().strip()
    n = max(1, min(int(n or 5), 15))
    rows = []
    if by in ("cpu", "processor"):
        for p in psutil.process_iter():                 # prime cpu counters
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        time.sleep(0.4)
        ncpu = psutil.cpu_count() or 1
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                cpu = p.cpu_percent(None) / ncpu        # normalize to total-machine %
                rss = p.info["memory_info"].rss if p.info.get("memory_info") else 0
                rows.append((p.info.get("name") or "?", cpu, rss))
            except Exception:
                continue
        rows.sort(key=lambda r: r[1], reverse=True)
        top = ["{} — {:.0f}% CPU ({:,.0f} MB)".format(nm, cpu, rss / 1e6) for nm, cpu, rss in rows[:n]]
        return "Top processes by CPU: " + "; ".join(top) + "."
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            rows.append((p.info.get("name") or "?", rss))
        except Exception:
            continue
    rows.sort(key=lambda r: r[1], reverse=True)
    top = ["{} — {:,.0f} MB".format(nm, rss / 1e6) for nm, rss in rows[:n]]
    return "Top processes by memory: " + "; ".join(top) + "."


# ----------------------- direct-answer fast path (no LLM round) -----------------------
# The router's is_machine_question() already identifies machine questions deterministically;
# these templates answer them by calling the telemetry DIRECTLY — ~1s instead of the 9-19s
# LLM tool-round. Returns None when unsure, so the normal LLM path stays the fallback.
_Q_FILES = re.compile(r"\bhow\s+many\s+(files?|folders?|items?)\b", re.I)
_Q_PATH = re.compile(r"\b(?:in|under|inside)\s+(?:my\s+)?([\w .\\/:~-]+?)(?:\s+folder|\s+directory)?\s*\??\s*$", re.I)
_Q_TOPPROC = re.compile(r"\b(?:using|eating|hogging|consuming|taking)\b.{0,24}\b(memory|ram|cpu|processor)\b"
                        r"|\bwhat(?:'s| is)\s+(?:process(?:es)?\s+)?(?:using|running|eating)\b", re.I)
_Q_CPU = re.compile(r"\b(cpu|processor)\b", re.I)
_Q_GPU = re.compile(r"\b(gpu|graphics\s*card|video\s*card)\b", re.I)
_Q_RAM = re.compile(r"\b(ram|memory)\b", re.I)
_Q_DISK = re.compile(r"\b(disk|storage|drive|space)\b", re.I)
_Q_OS = re.compile(r"\b(os|operating\s+system|windows\s+version)\b", re.I)
_Q_UP = re.compile(r"\b(uptime|been\s+(?:up|on|running)|turned\s+on)\b", re.I)
_Q_SPECS = re.compile(r"\b(specs?|hardware|system\s+info)\b", re.I)


def answer_machine_question(text: str) -> str | None:
    """Direct, natural-sounding answer to a machine question from live telemetry — no model
    round-trip. None when the question doesn't map cleanly (caller falls back to the LLM path)."""
    q = (text or "").strip()
    if _Q_FILES.search(q):
        m = _Q_PATH.search(q)
        return count_files(m.group(1).strip() if m else None)
    if _Q_TOPPROC.search(q):
        by = "cpu" if re.search(r"\bcpu|processor\b", q, re.I) else "memory"
        return get_top_processes(by, 3)
    t = system_telemetry()
    g = t["gpu"]
    if _Q_SPECS.search(q):
        return get_system_info()
    if _Q_GPU.search(q):
        if not g.get("available"):
            return "No GPU shows up via nvidia-smi right now."
        return (f"You've got an {g['name']} — {g['util']}% busy, "
                f"{g['vram_used']} of {g['vram_total']} MB VRAM in use, running {g['temp']}°C.")
    if _Q_CPU.search(q):
        return (f"You're on an {_cpu_name()} — {t['cpu']['count']} logical cores, "
                f"sitting at {t['cpu']['total']:.0f}% right now.")
    if _Q_RAM.search(q):
        return (f"{t['ram']['used']/1e9:.1f} of {t['ram']['total']/1e9:.1f} GB RAM in use "
                f"({t['ram']['percent']:.0f}%).")
    if _Q_DISK.search(q):
        free = (t['disk']['total'] - t['disk']['used']) / 1e9
        return (f"Disk C: has {free:.0f} GB free of {t['disk']['total']/1e9:.0f} GB "
                f"({t['disk']['percent']:.0f}% used).")
    if _Q_UP.search(q):
        up = t["uptime_s"]; d, h, m_ = up // 86400, (up % 86400) // 3600, (up % 3600) // 60
        span = f"{d} day{'s' if d != 1 else ''} and {h} hours" if d else f"{h} hours {m_} minutes"
        return f"This machine's been up {span}."
    if _Q_OS.search(q):
        return f"You're on {platform.platform()}."
    return None


_COMMON_DIRS = ("downloads", "documents", "desktop", "pictures", "videos", "music")


def count_files(path: str | None = None) -> str:
    """Count files + folders under a path (default: the user's home). Resolves a bare name like
    'Downloads' against home. Read-only: enumerates NAMES only, never opens a file. Time- and
    count-capped (~4s / 300k entries) so it can't hang on a huge tree; says so if it stops early."""
    home = pathlib.Path.home()
    if not path or not str(path).strip():
        root = home
    else:
        raw = str(path).strip().strip('"').strip("'")
        cand = pathlib.Path(raw).expanduser()
        if (not cand.is_absolute()) and (not cand.exists()):
            cand = home / raw                          # "Downloads" -> ~/Downloads
        root = cand
    if not root.exists():
        low = str(path).strip().lower()
        hint = f" (try one of: {', '.join(d.capitalize() for d in _COMMON_DIRS)})" if low in _COMMON_DIRS else ""
        return f"I don't see a folder at {root}.{hint}"
    if not root.is_dir():
        return f"{root} is a file, not a folder."
    deadline = time.time() + 4.0
    files = dirs = 0
    truncated = False
    stack = [str(root)]
    while stack:
        if time.time() > deadline or (files + dirs) > 300_000:
            truncated = True
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs += 1
                            stack.append(e.path)
                        else:
                            files += 1
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
    note = " — and that's a partial count; it's a big tree so I stopped early" if truncated else ""
    return f"{files:,} files and {dirs:,} folders under {root}{note}."

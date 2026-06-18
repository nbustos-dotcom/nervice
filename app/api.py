"""Nervice as a network API — a third face on the same brain (the local nervice.py / voice.py
loops are untouched and still direct-call). Reuses respond() + save_exchange(), so every existing
jail, scrub, gate, and timeout applies unchanged. Intended to be reached only over Nate's private
Tailscale tailnet (WireGuard-encrypted); bound to 127.0.0.1 for now. NEVER expose publicly.

Every endpoint requires:  Authorization: Bearer <NERVICE_API_TOKEN>
"""
import io
import os
import re
import sys
import wave
import uuid
import time
import base64
import secrets
import asyncio
import tempfile
import datetime
import importlib
import threading
import subprocess
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import pathlib

import numpy as np
from fastapi import (FastAPI, Depends, Header, HTTPException, UploadFile, File, Form,
                     WebSocket, WebSocketDisconnect)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from groq import RateLimitError
from app.chat import respond, save_exchange
from app.llm import rate_limit_message
from app.streaming import stream_reply
from app import usage   # Claude/Groq spend ledger + the in-code spend-guard state (precautions #2/#3)
from app import errorlog            # central error log + secret scrub (observability #1)
from app import screen_policy       # Phase-1 screen-control freeze source of truth (is_frozen/set/clear)
from app.turnlog import log_voice_timing   # persist voice stage timing to file (observability #3)

USER = "nate"
_TOKEN = os.environ.get("NERVICE_API_TOKEN")
WINDOW_MAX = 12

# Version banner (the stale-server killer): /health reports the git hash this PROCESS was booted
# from + the boot time, so a running server that predates HEAD is immediately visible.
_BOOT_TIME = datetime.datetime.now().isoformat(timespec="seconds")
try:
    _GIT_HASH = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=5,
                               cwd=str(pathlib.Path(__file__).resolve().parent.parent)).stdout.strip() or "unknown"
except Exception:
    _GIT_HASH = "unknown"


# Stale-server guard: the booted hash above is fixed for this process's life; _live_head() reads the
# CURRENT repo HEAD (cached ~30s) so /health can report whether the running server predates the latest
# commit. The HUD turns `stale: true` into a visible "restart to load new code" warning.
_HEAD_CACHE = {"hash": _GIT_HASH, "at": 0.0}


def _live_head() -> str:
    now = time.monotonic()
    if now - _HEAD_CACHE["at"] > 30.0:
        try:
            h = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=5,
                               cwd=str(pathlib.Path(__file__).resolve().parent.parent)).stdout.strip()
            if h:
                _HEAD_CACHE["hash"] = h
        except Exception:
            pass
        _HEAD_CACHE["at"] = now
    return _HEAD_CACHE["hash"]

# Per-conversation sliding window of the last WINDOW_MAX messages, in-process (single user).
# CAVEAT: lost on restart — but that's only the verbatim recent-turns buffer; durable facts about
# Nate live in the memory DB (retrieved fresh each turn), so a restart loses chat scrollback, not
# what Nervice knows about him.
_windows: dict[str, list] = {}
_pending: set = set()  # fire-and-forget save tasks (same pattern as the local loops)

# Voice-model warm state: cold -> warming -> warm (or failed: ...). Warmed in a background
# thread at startup so the first phone /voice call doesn't eat the 7-12s model load.
_voice_state = {"status": "cold"}


def _warm_voice():
    try:
        print("[api] voice models warming...", file=sys.stderr)
        t0 = time.time()
        importlib.import_module("app.voice")   # import lock makes concurrent /voice waits safe
        _voice_state["status"] = "warm"
        print(f"[api] voice models warm ({time.time() - t0:.1f}s)", file=sys.stderr)
    except Exception as e:
        _voice_state["status"] = f"failed: {repr(e)[:80]}"
        print(f"[api] voice warm FAILED: {repr(e)[:120]}", file=sys.stderr)


async def _db_keepalive(stop: asyncio.Event, interval_s: float = 60.0):
    """Ping the pooled Supabase connection so the remote pooler never idles it out — a real turn
    then pays warm retrieval (~0.3-0.8s) instead of the ~2.1s reconnect measured after idle.
    Ping errors are logged and the loop keeps going (pre_ping covers the next real checkout).
    Shutdown is an Event, NOT task.cancel(): cancelling mid-checkout rips through SQLAlchemy's
    greenlet bridge and leaks the connection — with the event, a mid-flight ping always completes
    and checks its connection back in before the loop exits."""
    from sqlalchemy import text
    from app.db import engine
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            break                       # stop requested while sleeping
        except asyncio.TimeoutError:
            pass                        # interval elapsed — ping
        try:
            async with engine.connect() as c:
                await c.execute(text("SELECT 1"))
        except Exception as e:
            print(f"[api] db keepalive failed: {repr(e)[:80]}", file=sys.stderr)


@asynccontextmanager
async def lifespan(_app):
    _voice_state["status"] = "warming"
    threading.Thread(target=_warm_voice, daemon=True).start()
    stop = asyncio.Event()
    keepalive = asyncio.create_task(_db_keepalive(stop))
    try:
        yield
    finally:
        try:
            from app import browser
            await browser.close_browser()                   # never leave an orphan chromium on exit
        except Exception:
            pass
        stop.set()
        try:
            await asyncio.wait_for(keepalive, timeout=10)   # let a mid-flight ping finish
        except (asyncio.TimeoutError, asyncio.CancelledError):
            keepalive.cancel()                              # hung network — last resort



app = FastAPI(title="Nervice API", lifespan=lifespan)

# The phone client (static files) is served UNauthenticated — acceptable ONLY because the page is
# inert without a token and is reachable only over the private tailnet. Every API route below keeps
# its Bearer auth; the static mount does not bypass it.
import mimetypes
mimetypes.add_type("application/manifest+json", ".webmanifest")   # StaticFiles consults mimetypes
_STATIC = pathlib.Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/sw.js")
async def service_worker():
    """The PWA service worker, served at the TOP level so its scope covers /v3 (a worker served
    under /static could only control /static/*). __VER__ becomes this process's git hash, so each
    commit gets a fresh cache name. Like the static pages: unauthenticated but inert — the worker
    only ever caches icons + the manifest, never API responses or anything authed."""
    from fastapi.responses import Response
    js = (_STATIC / "sw.js").read_text(encoding="utf-8").replace("__VER__", _GIT_HASH)
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})   # browser revalidates the SW itself


@app.get("/")
async def index():
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/v2")
async def index_v2():
    # New dashboard client. Like /, served UNauthenticated (inert without a token); every data
    # route it calls (/weather, /activity, /health, /ws/*) keeps its Bearer auth.
    return FileResponse(str(_STATIC / "index_v2.html"))


@app.get("/v3")
async def index_v3():
    # JARVIS HUD command center. Like / and /v2, served UNauthenticated (inert without a token);
    # every data route it calls (/system, /weather, /nervice-stats, /activity, /news, /health,
    # /ws/*) keeps its Bearer auth.
    return FileResponse(str(_STATIC / "index_v3.html"))


async def auth(authorization: str = Header(None)):
    """Bearer-token gate on every route. Constant-time compare; reject missing/wrong with 401."""
    if not _TOKEN:
        raise HTTPException(status_code=500, detail="NERVICE_API_TOKEN not configured on the server")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not secrets.compare_digest(authorization[7:], _TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


def _store(conversation_id: str, user_message: str, reply: str) -> None:
    """Fire-and-forget persistence so the response returns immediately; errors logged, never raised."""
    async def _run():
        try:
            await save_exchange(USER, conversation_id, user_message, reply)
        except Exception as e:
            print(f"[api store failed] {repr(e)[:120]}", file=sys.stderr)
    t = asyncio.create_task(_run())
    _pending.add(t)
    t.add_done_callback(_pending.discard)


def _advance_window(cid: str, user_message: str, reply: str) -> None:
    w = _windows.setdefault(cid, [])
    w.append({"role": "user", "content": user_message})
    w.append({"role": "assistant", "content": reply})
    _windows[cid] = w[-WINDOW_MAX:]


class ChatIn(BaseModel):
    message: str
    conversation_id: str | None = None


@app.get("/health", dependencies=[Depends(auth)])
async def health():
    # Voice models are warmed by the startup thread; report where that stands without forcing a load.
    voice = _voice_state["status"]
    live = _live_head()
    stale = (_GIT_HASH != "unknown" and live != "unknown" and live != _GIT_HASH)
    if voice == "warm" and "app.voice" in sys.modules:
        v = sys.modules["app.voice"]
        return {"status": "ok", "voice": "warm", "stt": v.WHISPER_PATH, "tts": v.TTS_ENGINE,
                "git": _GIT_HASH, "head": live, "stale": stale, "boot": _BOOT_TIME}
    return {"status": "ok", "voice": voice, "stt": "faster-whisper base.en", "tts": "kokoro",
            "git": _GIT_HASH, "head": live, "stale": stale, "boot": _BOOT_TIME}


# Intentionally UNAUTHENTICATED so it can be opened in a plain browser on the REAL Tauri-launched
# (console-less) server — the one environment that can't be recreated from a bash console. Read-only,
# NON-BILLABLE (spawns claude.exe --version only, never a query), returns non-sensitive diagnostic
# data (versions, the CLI path, the spawn result). Same localhost/tailnet bind as the static pages,
# which are also unauthenticated. Does NOT touch the SDK query() path, the spend guard, or any creds.
@app.get("/diag/claude-spawn")
async def diag_claude_spawn():
    """NON-BILLABLE SDK-spawn self-test: runs the bundled claude.exe `--version` via the SDK's
    anyio.open_process mechanism (both stderr variants) so the WinError 50 console-less spawn failure
    can be checked from the real Tauri server. Never sends a prompt; no tokens; no state change."""
    from app import diag
    return await diag.claude_spawn_selftest()


@app.get("/weather", dependencies=[Depends(auth)])
async def weather():
    """Real current weather + multi-day forecast for the stored location (phone GPS if sent, else
    the Orland Hills default). Includes `place`. {available:false} on upstream failure — never faked."""
    from app.weather import get_forecast
    d = await get_forecast(7)
    return {"available": bool(d), **(d or {})}


class LocationIn(BaseModel):
    lat: float
    lon: float


@app.post("/location", dependencies=[Depends(auth)])
async def set_location_ep(inp: LocationIn):
    """The phone reports its real GPS here; weather then uses it. Reverse-geocoded for display."""
    from app.geo import set_location
    if not (-90 <= inp.lat <= 90 and -180 <= inp.lon <= 180):
        raise HTTPException(status_code=422, detail="lat/lon out of range")
    rec = await set_location(inp.lat, inp.lon)
    return {"ok": True, "name": rec["name"], "lat": rec["lat"], "lon": rec["lon"]}


@app.get("/location", dependencies=[Depends(auth)])
async def get_location_ep():
    """Current stored location (phone GPS or default) for the settings UI."""
    from app.geo import load_location
    loc = load_location()
    return {"lat": loc["lat"], "lon": loc["lon"], "name": loc.get("name"), "source": loc.get("source", "default")}


# ----------------------------- /system : real machine telemetry -----------------------------
from app.sysinfo import system_telemetry   # shared with the conversational system-awareness tools


@app.get("/system", dependencies=[Depends(auth)])
async def system():
    """Real machine telemetry — psutil (CPU/RAM/swap/disk/net/uptime/procs) + nvidia-smi (GPU).
    Cheap to poll every ~2s; the CPU sample blocks ~0.15s in a worker thread, not the loop."""
    return await asyncio.to_thread(system_telemetry)


# --------------- /updates : real git history (what's changed about Nervice) ---------------
_REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
_updates_cache = {"t": 0.0, "items": []}
_COMMIT_PREFIX = re.compile(r"^[\w()./,-]{1,24}:\s+")   # strip "ui(v3): " / "router: " prefixes


@app.get("/updates", dependencies=[Depends(auth)])
async def updates():
    """Last ~8 commits as {date, summary} — REAL git log only (cached 120s). Empty list (panel
    hides) if git is unavailable. No hashes, no fabricated changelog."""
    if time.time() - _updates_cache["t"] < 120 and _updates_cache["items"]:
        return {"items": _updates_cache["items"]}
    def _git():
        try:
            out = subprocess.run(
                ["git", "log", "-8", "--date=format:%b %d", "--format=%ad|%s"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, cwd=str(_REPO_DIR)).stdout   # git emits UTF-8; never decode with the locale codepage
            items = []
            for line in out.splitlines():
                if "|" not in line:
                    continue
                date, summary = line.split("|", 1)
                summary = _COMMIT_PREFIX.sub("", summary).strip()
                items.append({"date": date.strip(), "summary": summary[:90]})
            return items
        except Exception:
            return []
    items = await asyncio.to_thread(_git)
    if items:
        _updates_cache.update(t=time.time(), items=items)
    return {"items": items}


# --------------- /last-turn : which brain answered + how long (from turns.log) ---------------
_TURNS_LOG = _REPO_DIR / "logs" / "turns.log"
_TURN_LINE = re.compile(r"^(\S+ \S+)\troute=(\S+)\trung=(\S+)\t([\d.]+)s\tpath=(\S+)")


@app.get("/last-turn", dependencies=[Depends(auth)])
async def last_turn():
    """The most recent turn's REAL telemetry from logs/turns.log: which rung answered, the route,
    and the wall-clock seconds. {available:false} when no turn has been logged yet."""
    def _tail():
        try:
            with open(_TURNS_LOG, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                lines = f.read().decode("utf-8", "replace").strip().splitlines()
            for line in reversed(lines):
                m = _TURN_LINE.match(line.strip())
                if m:
                    return {"available": True, "ts": m.group(1), "route": m.group(2),
                            "rung": m.group(3), "seconds": float(m.group(4)), "path": m.group(5)}
        except Exception:
            pass
        return {"available": False}
    return await asyncio.to_thread(_tail)


@app.get("/nervice-stats", dependencies=[Depends(auth)])
async def nervice_stats():
    """Nervice's own real internals: active memory count, total messages stored, today's messages,
    and the live voice engine + warm state. Only what is genuinely queryable."""
    from sqlalchemy import select, func
    from app.db import AsyncSessionLocal
    from app.models import Memory, Message
    midnight = datetime.datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as s:
        memories = (await s.execute(select(func.count()).select_from(Memory).where(
            Memory.user_id == USER, Memory.is_active == True))).scalar()      # noqa: E712
        messages = (await s.execute(select(func.count()).select_from(Message).where(
            Message.user_id == USER))).scalar()
        today = (await s.execute(select(func.count()).select_from(Message).where(
            Message.user_id == USER, Message.created_at >= midnight))).scalar()
    voice = _voice_state["status"]
    if voice == "warm" and "app.voice" in sys.modules:
        v = sys.modules["app.voice"]
        engine, stt = v.TTS_ENGINE, v.WHISPER_PATH
    else:
        engine, stt = "kokoro", "faster-whisper base.en"
    # Lean real metrics: today's turn count (turns.log) + today's Groq token usage (usage.json).
    from app import usage
    today_str = datetime.datetime.now(_TZ).strftime("%Y-%m-%d")

    def _turns_today() -> int:
        try:
            lines = _TURNS_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
            return sum(1 for ln in lines if ln[:10] == today_str and "route=" in ln)
        except Exception:
            return 0

    turns_today = await asyncio.to_thread(_turns_today)
    return {"memories": int(memories or 0), "messages": int(messages or 0), "today": int(today or 0),
            "voice": voice, "engine": engine, "stt": stt,
            "turns_today": turns_today, "tokens_today": usage.today_groq_tokens()}


# ----------------------------- /news : optional, BBC RSS, real headlines only -----------------------------
_news_cache = {"t": 0.0, "items": []}


def _rss_age(pubdate: str) -> str:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pubdate)
        s = max(0, int(time.time() - dt.timestamp()))
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
    except Exception:
        return ""


async def _fetch_news() -> list[dict]:
    if time.time() - _news_cache["t"] < 600 and _news_cache["items"]:
        return _news_cache["items"]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as c:
            r = await c.get("https://feeds.bbci.co.uk/news/rss.xml", headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return _news_cache["items"]
        items = []
        for it in re.findall(r"<item>(.*?)</item>", r.text, re.S)[:8]:
            tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
            pd = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            if tm and tm.group(1).strip():
                items.append({"title": " ".join(tm.group(1).split())[:110],
                              "ago": _rss_age(pd.group(1) if pd else "")})
        if items:
            _news_cache.update(t=time.time(), items=items)
        return items
    except Exception:
        return _news_cache["items"]   # serve last-good on a hiccup; never fabricate


@app.get("/news", dependencies=[Depends(auth)])
async def news():
    """Real top headlines (BBC News RSS, keyless, cached 10 min). source labels the origin so the
    UI never implies these are Nervice's words. Empty list on persistent failure — no fake items."""
    return {"source": "BBC News", "items": await _fetch_news()}


# ---- activity feed: REAL recent activity merged from three live sources, newest first ----
_TZ = ZoneInfo("America/Chicago")
_REPO = pathlib.Path(__file__).resolve().parent.parent


def _ago(now: float, ts: float) -> str:
    s = max(0, int(now - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _log_epoch(iso: str) -> float:
    """The audit logs write naive local-time ISO; interpret as America/Chicago for sorting."""
    try:
        return datetime.datetime.fromisoformat(iso).replace(tzinfo=_TZ).timestamp()
    except Exception:
        return 0.0


def _short(s: str, n: int = 46) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


def _prettify_url(s: str) -> str:
    return re.sub(r"https?://(?:www\.)?([a-z0-9-]+)\.[a-z.]+\S*",
                  lambda m: m.group(1).capitalize(), s, flags=re.I)


def _read_tail(path: pathlib.Path, n: int = 40) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


async def _activity_items(limit: int = 8) -> list[dict]:
    items: list[tuple[float, str, str]] = []   # (epoch, kind, text)
    # 1. local-control audit — completed actions only (skip ASK/CANCEL/ABANDON)
    for ln in _read_tail(_REPO / "logs" / "computer_actions.log"):
        f = ln.split("\t")
        if len(f) >= 3 and f[1] in ("EXEC", "CONFIRM-EXEC"):
            result = f[-1].strip()
            if result:
                kind = ("screenshot" if "screenshot" in result.lower()
                        else "open" if "open" in result.lower() else "do")
                items.append((_log_epoch(f[0]), kind, _prettify_url(result).rstrip(".")))
    # 2. agent calls (browse / build / self-update / consult)
    for ln in _read_tail(_REPO / "logs" / "claude_calls.log"):
        f = ln.split("\t")
        if len(f) < 3:
            continue
        ts, kind, task = f[0], f[1], f[-1].strip()
        if kind == "BROWSE":
            items.append((_log_epoch(ts), "browse", "Browsed: " + _short(task)))
        elif kind == "BUILD":
            items.append((_log_epoch(ts), "build", "Built: " + _short(task)))
        elif kind == "SELFMOD":
            items.append((_log_epoch(ts), "selfmod", "Drafted a self-update"))
        elif kind == "CLAUDE":
            items.append((_log_epoch(ts), "think", "Consulted Claude"))
    # 3. recent questions from the conversation DB
    try:
        from sqlalchemy import select
        from app.db import AsyncSessionLocal
        from app.models import Message
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(
                select(Message).where(Message.user_id == USER, Message.role == "user")
                .order_by(Message.created_at.desc()).limit(10))).scalars().all()
        for m in rows:
            if m.created_at:
                items.append((m.created_at.timestamp(), "ask", "Asked: " + _short(m.content)))
    except Exception as e:
        print(f"[activity db] {repr(e)[:80]}", file=sys.stderr)

    items.sort(key=lambda x: x[0], reverse=True)
    now = time.time()
    out, seen = [], set()
    for ep, kind, text in items:
        if text in seen:
            continue
        seen.add(text)
        out.append({"text": text, "kind": kind, "ago": _ago(now, ep)})
        if len(out) >= limit:
            break
    return out


@app.get("/activity", dependencies=[Depends(auth)])
async def activity():
    """REAL recent activity — local-control audit log + agent calls + recent questions, newest
    first. No fabrication: if a source is empty there are simply fewer items."""
    return {"items": await _activity_items()}


@app.get("/orchestrator/state", dependencies=[Depends(auth)])
async def orchestrator_state():
    """REAL orchestrator cockpit for the Nerve Pages PROJECT panel: the project goal, critique gaps,
    the ordered steps with done/current/pending status, and the CACHED current-step Claude Code
    prompt. Read-only, no LLM. has_doc is False when the project doc is missing or unfilled."""
    from app import orchestrator
    return await asyncio.to_thread(orchestrator.state_snapshot)


@app.get("/orchestrator/loop", dependencies=[Depends(auth)])
async def orchestrator_loop():
    """REAL file-loop view for the Nerve Pages LOOP panel: everything /orchestrator/state has PLUS the
    handed-off step prompt, Claude Code's last result (.nervice/result.json), the read-only git
    cross-check verdict, and whether a proposal is awaiting yes/no. Read-only, no LLM, no mutation;
    honest empties (no workspace / no result) instead of fabrication."""
    from app import orchestrator
    return await asyncio.to_thread(orchestrator.loop_snapshot)


@app.get("/actions/feed", dependencies=[Depends(auth)])
async def actions_feed(limit: int = 40):
    """REAL action audit for the ACTIONS panel: computer_actions.log + browser_actions.log merged,
    newest first (timestamp, action, target, outcome). No fabrication."""
    from app import actionlog
    limit = max(1, min(int(limit), 100))
    return {"items": await asyncio.to_thread(actionlog.read_feed, limit)}


# ================== HUD INFORMATION LAYER: Nervice's real internal state ==================
# Four read-only streams + one grounded daily line. Everything here reports what already
# exists (DB rows, turns.log, proposals/, memory_requeue.jsonl) — nothing is fabricated.

@app.get("/memories/recent", dependencies=[Depends(auth)])
async def memories_recent(n: int = 8):
    """Active memories as a newest + highest-salience mix: half the slots go to the most recent,
    the rest to the highest-salience not already included. No embeddings, no IDs — display only."""
    n = max(1, min(int(n), 20))
    from sqlalchemy import select
    from app.db import AsyncSessionLocal
    from app.models import Memory
    async with AsyncSessionLocal() as s:
        newest = (await s.execute(
            select(Memory).where(Memory.user_id == USER, Memory.is_active == True)   # noqa: E712
            .order_by(Memory.created_at.desc()).limit(n))).scalars().all()
        salient = (await s.execute(
            select(Memory).where(Memory.user_id == USER, Memory.is_active == True)   # noqa: E712
            .order_by(Memory.salience.desc(), Memory.created_at.desc()).limit(n))).scalars().all()
    now = time.time()
    out, seen = [], set()
    half = (n + 1) // 2
    for m in list(newest[:half]) + salient + newest:    # newest half first, then top-salience fill
        if m.id in seen or len(out) >= n:
            continue
        seen.add(m.id)
        age = _ago(now, m.created_at.timestamp()) if m.created_at else ""
        out.append({"content": m.content, "category": m.category,
                    "salience": int(m.salience), "age_human": age})
    return {"items": out}


def _today_lines(prefix: str) -> list:
    """turns.log lines whose timestamp starts with the given YYYY-MM-DD prefix, parsed."""
    rows = []
    try:
        for line in _TURNS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith(prefix):
                continue
            m = _TURN_LINE.match(line.strip())
            if m:
                rows.append({"route": m.group(2), "rung": m.group(3), "s": float(m.group(4))})
    except Exception:
        pass
    return rows


_LLM_RUNGS = {"groq", "ollama", "claude-pro", "claude-max", "extractive", "exhausted"}


@app.get("/ladder", dependencies=[Depends(auth)])
async def ladder():
    """Live brain-ladder state. groq: inferred from the LAST turn that involved an LLM rung —
    a turn that laddered past Groq (ollama/claude/extractive/exhausted) means capped; there is
    no header probing (the residue effect makes tiny probes lie). ollama: a real is_up() ping.
    claude: how many account config dirs exist. today: real per-rung distribution from turns.log."""
    from app import ollama_client
    from app import llm as _llm
    from app.agent import CLAUDE_ACCOUNTS
    today = datetime.datetime.now(_TZ).strftime("%Y-%m-%d")
    rows = await asyncio.to_thread(_today_lines, today)
    groq = "up"
    for r in reversed(rows):
        if r["rung"] in _LLM_RUNGS:
            groq = "up" if r["rung"] == "groq" else "capped"
            break
    # Fix 2.3: the sticky cap-state is authoritative while active — and it carries the until time.
    cu = _llm.capped_until()
    if cu:
        groq = "capped"
    capped_until_iso = (datetime.datetime.fromtimestamp(cu, _TZ).isoformat(timespec="seconds")
                        if cu else None)
    try:
        ollama_up = await asyncio.wait_for(ollama_client.is_up(), timeout=3)
    except Exception:
        ollama_up = False
    accounts = sum(1 for _name, d in CLAUDE_ACCOUNTS if pathlib.Path(d).expanduser().is_dir())
    dist: dict = {}
    for r in rows:
        d = dist.setdefault(r["rung"], {"count": 0, "_sum": 0.0})
        d["count"] += 1
        d["_sum"] += r["s"]
    for rung, d in dist.items():
        d["avg_s"] = round(d.pop("_sum") / d["count"], 2)
    return {"groq": groq, "ollama": bool(ollama_up), "claude": accounts,
            "capped_until": capped_until_iso,
            "claude_spend_usd": round(usage.today_claude_usd(), 4),
            "claude_cap_usd": usage.CLAUDE_DAILY_CAP_USD,
            "free_only": usage.free_only(),
            "today": {"turns": len(rows), "rungs": dist}}


# --------------- observability: queryable turn history + recent errors ---------------
def _parse_turn_ts(s: str):
    """turns.log asctime -> NAIVE local datetime (matches datetime.now() for window math, since the
    logging module timestamps in local time). Returns None for an unparseable stamp (malformed line)."""
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _tail_text_lines(path: pathlib.Path, max_bytes: int = 2_000_000) -> list:
    """Last max_bytes of a (possibly large/rotating) log as decoded lines — a bounded read that can't
    hang or blow memory on a huge file. The window we aggregate is recent, so the tail is sufficient."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def _history(hours: float) -> dict:
    """Honest aggregates over the last `hours` of turns.log: turn count, rung + route distributions,
    avg / p95 / max latency, and the error count from errors.log. Tail-bounded reads; malformed and
    non-turn (log_event) lines are skipped. Empty/missing log -> honest zeros, never a crash."""
    import math
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
    secs, rungs, routes, count = [], {}, {}, 0
    for line in _tail_text_lines(_TURNS_LOG):
        m = _TURN_LINE.match(line.strip())
        if not m:
            continue                                   # malformed / non-turn line skipped gracefully
        ts = _parse_turn_ts(m.group(1))
        if ts is None or ts < cutoff:
            continue
        count += 1
        routes[m.group(2)] = routes.get(m.group(2), 0) + 1
        rungs[m.group(3)] = rungs.get(m.group(3), 0) + 1
        secs.append(float(m.group(4)))
    secs.sort()

    def pctl(p: float) -> float:
        if not secs:
            return 0.0
        k = max(1, math.ceil(p / 100.0 * len(secs)))   # nearest-rank percentile
        return round(secs[k - 1], 2)

    avg = round(sum(secs) / len(secs), 2) if secs else 0.0
    return {
        "window_hours": round(hours, 2),
        "turns": count,
        "rungs": rungs,
        "routes": routes,
        "latency": {"avg_s": avg, "p95_s": pctl(95), "max_s": round(secs[-1], 2) if secs else 0.0},
        "errors": errorlog.count_since(cutoff.isoformat(timespec="seconds")),
    }


@app.get("/stats/history", dependencies=[Depends(auth)])
async def stats_history(hours: float = 24.0, days: float | None = None):
    """Honest aggregates over a recent window of logs/turns.log (the per-turn telemetry already
    logged — NO new writes): turn count, rung distribution, route distribution, avg + p95 latency,
    and error count (logs/errors.log). days=N overrides hours. Tail-bounded — safe on a large log."""
    if days is not None:
        hours = max(0.0, float(days)) * 24.0
    hours = max(0.0, min(float(hours), 24.0 * 90))      # clamp to 90 days
    return await asyncio.to_thread(_history, hours)


@app.get("/errors/recent", dependencies=[Depends(auth)])
async def errors_recent(n: int = 20):
    """The last N entries from logs/errors.log (NEWEST first) — notable/handled failures captured
    across the turn paths, context already scrubbed of secrets. Read-only; no fabrication."""
    n = max(1, min(int(n), 100))
    return {"items": await asyncio.to_thread(errorlog.read_recent, n)}


class FreeOnlyIn(BaseModel):
    on: bool


@app.post("/free-only", dependencies=[Depends(auth)])
async def free_only_toggle(inp: FreeOnlyIn):
    """Flip FREE-ONLY mode. When ON, every Claude path short-circuits to the free rungs (or an
    honest 'Claude is off') — enforced in code (app/usage.claude_blocked_reason, checked at every
    Claude entry point in app/agent.py), independent of any Anthropic-account setting. Persisted in
    data/claude_control.json. (A HUD button can wire here later; the endpoint + GET /ladder state
    are the contract now.)"""
    usage.set_free_only(bool(inp.on))
    return {"free_only": usage.free_only()}


@app.get("/pending", dependencies=[Depends(auth)])
async def pending():
    """What's waiting on Nate: selfmod proposals still pending (real records in proposals/) and
    the memory re-verification queue counts by kind (data/memory_requeue.jsonl). Empty = silence."""
    def _scan():
        props = []
        try:
            import json as _json
            for p in sorted((_REPO / "proposals").glob("*.json")):
                try:
                    d = _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if d.get("status") == "pending":
                    title = " ".join(str(d.get("instruction", "")).split())[:80]
                    props.append({"id": d.get("id", p.stem), "title": title})
        except Exception:
            pass
        requeue: dict = {}
        try:
            import json as _json
            with open(_REPO / "data" / "memory_requeue.jsonl", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        k = _json.loads(line).get("kind", "?")
                        requeue[k] = requeue.get(k, 0) + 1
        except Exception:
            pass
        return {"proposals": props, "requeue": requeue}
    return await asyncio.to_thread(_scan)


# --------------- Self-modification proposals — the ASKS review/approve surface ---------------
# Review + approve/reject the selfmod proposals Nate has pending. Approval routes THROUGH app/selfmod's
# existing gate (re-validate paths -> dry-run -> apply -> compile -> assert SAFETY_FLOOR in PERSONA ->
# commit -> rollback-on-any-failure). This layer only LISTS and forwards to selfmod — it NEVER bypasses
# the gate, never auto-approves, and app/selfmod.py stays zero-diff.

_PID_RE = re.compile(r"^[0-9][0-9-]{6,40}$")   # selfmod ids are strftime '%Y%m%d-%H%M%S-%f' (digits+hyphens)


def _proposal_view(rec: dict) -> dict:
    """Shape a selfmod record + its ACTUAL diff for the ASKS panel (Nate approves INFORMED, never
    blind). Reads proposals/<id>.patch via selfmod.PROPOSALS_DIR. Read-only; never raises."""
    from app import selfmod
    pid = str(rec.get("id", ""))
    diff = ""
    try:
        pf = selfmod.PROPOSALS_DIR / f"{pid}.patch"
        if _PID_RE.match(pid) and pf.exists():
            diff = pf.read_text(encoding="utf-8")
    except Exception:
        diff = ""
    return {"id": pid, "paths": rec.get("paths", []), "created": rec.get("created", ""),
            "instruction": " ".join(str(rec.get("instruction", "")).split())[:300],
            "summary": " ".join(str(rec.get("summary", "")).split())[:600],
            "status": rec.get("status", ""), "reason": rec.get("reason", ""),
            "diff": diff[:20000]}


@app.get("/proposals", dependencies=[Depends(auth)])
async def proposals_list(history: bool = False):
    """Pending self-update proposals with id, target files, timestamp, summary, and the ACTUAL diff.
    Default: status=pending only; ?history=true includes applied/rejected. Read-only, no mutation."""
    def _scan():
        from app import selfmod
        recs = selfmod.list_proposals()
        if not history:
            recs = [r for r in recs if r.get("status") == "pending"]
        recs.sort(key=lambda r: str(r.get("created", "")), reverse=True)   # newest first
        return [_proposal_view(r) for r in recs]
    return {"proposals": await asyncio.to_thread(_scan)}


@app.post("/proposals/{pid}/approve", dependencies=[Depends(auth)])
async def proposals_approve(pid: str):
    """Approve a pending proposal — routes THROUGH selfmod.apply() (the full gate, UNCHANGED): path
    re-validation, dry-run, compile, the SAFETY_FLOOR assertion, commit, and rollback on any failure.
    Returns {applied, message}; a gate block reports its reason verbatim. NEVER bypasses the gate."""
    if not _PID_RE.match(pid or ""):
        raise HTTPException(status_code=400, detail="bad proposal id")
    from app import selfmod
    ok, message = await asyncio.to_thread(selfmod.apply, pid)
    print(f"[asks] approve {pid} -> {'APPLIED' if ok else 'blocked'}", file=sys.stderr)
    return {"applied": bool(ok), "message": message}


@app.post("/proposals/{pid}/reject", dependencies=[Depends(auth)])
async def proposals_reject(pid: str):
    """Reject/discard a pending proposal — selfmod.reject() (marks it rejected). No code is touched."""
    if not _PID_RE.match(pid or ""):
        raise HTTPException(status_code=400, detail="bad proposal id")
    from app import selfmod
    ok, message = await asyncio.to_thread(selfmod.reject, pid)
    return {"rejected": bool(ok), "message": message}


_DAILY_CACHE = _REPO / "data" / "daily_summary.json"


# Only these rungs are BRAINS (LLM engines). control/skill/direct/music/extractive/exhausted
# are routes or non-LLM mechanisms — they must never be phrased as the thing that "handled" turns.
_BRAIN_RUNGS = {"groq", "ollama", "claude-pro", "claude-max"}
_BAD_HANDLER = re.compile(r"\b(?:handled\s+by|by)\s+(?:[\w\s,-]*\b)?"
                          r"(control|skill|direct|music|extractive|exhausted)\b", re.I)


def _daily_template(facts: dict) -> str:
    """Deterministic sentence from the aggregates — the no-LLM fallback. Never invents; the
    handled-by clause lists brains only (the non-brain remainder is implicit in the total)."""
    if not facts["turns"]:
        return f"Yesterday ({facts['date']}): no turns."
    head = f"Yesterday: {facts['turns']} turns"
    if facts["brains"]:
        b = ", ".join(f"{c} by {r}" for r, c in sorted(facts["brains"].items(), key=lambda x: -x[1]))
        head += f" ({b})"
    mem = (f"{facts['memories_added']} memories added" if facts["memories_added"] != 1
           else "1 memory added")
    return f"{head}; {mem}."


@app.get("/daily-summary", dependencies=[Depends(auth)])
async def daily_summary():
    """ONE sentence built from YESTERDAY's real aggregates (turn count, rung split, memories
    added). Groq phrases it under a strict facts-only prompt; any failure falls back to a
    deterministic template over the SAME numbers. Cached per-day in data/daily_summary.json —
    the polls hit the cache, never the LLM."""
    import json as _json
    today_key = datetime.datetime.now(_TZ).strftime("%Y-%m-%d")
    try:
        cached = _json.loads(_DAILY_CACHE.read_text(encoding="utf-8"))
        if cached.get("date") == today_key and cached.get("sentence"):
            return {"available": True, "sentence": cached["sentence"], "date": cached["of"]}
    except Exception:
        pass
    # ---- yesterday's REAL aggregates ----
    y_mid = datetime.datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    y_start = y_mid - datetime.timedelta(days=1)
    y_key = y_start.strftime("%Y-%m-%d")
    rows = await asyncio.to_thread(_today_lines, y_key)
    rung_counts: dict = {}
    for r in rows:
        rung_counts[r["rung"]] = rung_counts.get(r["rung"], 0) + 1
    mems = 0
    try:
        from sqlalchemy import select, func
        from app.db import AsyncSessionLocal
        from app.models import Memory
        async with AsyncSessionLocal() as s:
            mems = (await s.execute(select(func.count()).select_from(Memory).where(
                Memory.user_id == USER, Memory.created_at >= y_start,
                Memory.created_at < y_mid))).scalar() or 0
    except Exception:
        pass
    brains = {r: c for r, c in rung_counts.items() if r in _BRAIN_RUNGS}
    other = {r: c for r, c in rung_counts.items() if r not in _BRAIN_RUNGS}
    facts = {"date": y_key, "turns": len(rows), "brains": brains,
             "non_llm_turns": other, "memories_added": int(mems)}
    sentence = None
    if facts["turns"] or facts["memories_added"]:
        try:
            from app.llm import chat_json
            out = await chat_json(
                "You write ONE short factual sentence (max 28 words) summarizing yesterday's "
                "assistant activity for a status display. Cover the turn count, which brains "
                "handled them, and memories added if nonzero. The 'handled by' clause may ONLY "
                "name entries from `brains` (LLM engines); `non_llm_turns` are routes/mechanisms, "
                "NOT handlers — mention them, if at all, only as separate counts. Use ONLY the "
                'numbers provided — nothing not in the data. Return JSON: {"sentence": "..."}',
                _json.dumps(facts))
            cand = (out or {}).get("sentence", "")
            if (isinstance(cand, str) and 10 <= len(cand) <= 220
                    and not _BAD_HANDLER.search(cand)):   # grounding guard: routes are never handlers
                sentence = cand.strip()
        except Exception:
            sentence = None
    if not sentence:
        sentence = _daily_template(facts)
    try:
        _DAILY_CACHE.parent.mkdir(exist_ok=True)
        _DAILY_CACHE.write_text(_json.dumps(
            {"date": today_key, "of": y_key, "sentence": sentence, "facts": facts}),
            encoding="utf-8")
    except Exception:
        pass
    return {"available": True, "sentence": sentence, "date": y_key}


@app.post("/chat", dependencies=[Depends(auth)])
async def chat(inp: ChatIn):
    cid = inp.conversation_id or str(uuid.uuid4())
    window = _windows.setdefault(cid, [])
    try:
        reply = await respond(USER, inp.message, window, voice_mode=False)
    except RateLimitError as e:   # backstop — respond() already handles 429, this guards any new path
        return {"reply": rate_limit_message(e), "conversation_id": cid}
    except Exception as e:        # observability #1: record any turn failure (route errors tagged
        errorlog.log_error("respond", e, inp.message)   # deeper aren't double-logged), then re-raise
        raise
    _advance_window(cid, inp.message, reply)
    _store(cid, inp.message, reply)
    return {"reply": reply, "conversation_id": cid}


@app.post("/voice", dependencies=[Depends(auth)])
async def voice(audio: UploadFile = File(...), conversation_id: str | None = Form(None)):
    # Models are normally warm (startup thread); if a call lands mid-warm, this import blocks on
    # the import lock until ready instead of failing. Text-only API use never needs them.
    t_total = time.time()
    import app.voice as v

    data = await audio.read()
    suffix = os.path.splitext(audio.filename or "")[1] or ".bin"
    tf = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    t0 = time.time()
    try:
        tf.write(data)
        tf.close()
        clip = v.transcribe_clip(tf.name)   # decode -> Silero VAD gate -> STT with confidence
    finally:
        os.unlink(tf.name)
    stt_s = time.time() - t0
    transcript = clip["text"]

    cid = conversation_id or str(uuid.uuid4())
    # Silent drop (NO turn, NO memory, NO spoken nudge) when Silero hears no speech (room noise /
    # silence the browser VAD let through) or the transcript is junk (empty / punct-only / filler /
    # low-confidence). The phone just gets an empty reply — the prior no-speech response shape.
    if not clip["speech"]:
        print(f"[voice vad-gate] silero: no speech -> silent drop "
              f"(stt={stt_s:.2f}s total={time.time()-t_total:.2f}s)", file=sys.stderr)
        return {"transcript": "", "reply": "", "conversation_id": cid, "audio_wav_base64": ""}
    if clip["junk"]:
        print(f"[voice junk-gate] transcript={transcript!r} nsp={clip['no_speech_prob']:.2f} "
              f"alp={clip['avg_logprob']:.2f} -> silent drop (no turn)", file=sys.stderr)
        return {"transcript": transcript, "reply": "", "conversation_id": cid, "audio_wav_base64": ""}

    window = _windows.setdefault(cid, [])
    t0 = time.time()
    try:
        reply = await respond(USER, transcript, window, voice_mode=True)
    except RateLimitError as e:   # backstop — reply still gets synthesized below so the phone speaks it
        reply = rate_limit_message(e)
    except Exception as e:        # observability #1: record, then re-raise (unchanged failure behavior)
        errorlog.log_error("respond:voice", e, transcript)
        raise
    llm_s = time.time() - t0
    _advance_window(cid, transcript, reply)
    _store(cid, transcript, reply)

    # Synthesize the reply to a WAV and return it base64-encoded in the JSON. The phone client
    # decodes audio_wav_base64 -> bytes -> Blob({type:'audio/wav'}) and plays it.
    t0 = time.time()
    pcm, sr = v.synth_to_pcm(reply)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    audio_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    tts_s = time.time() - t0
    total_s = time.time() - t_total
    print(f"[voice-timing] stt={stt_s:.2f}s llm={llm_s:.2f}s tts={tts_s:.2f}s "
          f"total={total_s:.2f}s", file=sys.stderr)
    log_voice_timing(stt_s, llm_s, tts_s, total_s)   # observability #3: persist the stage breakdown
    return {"transcript": transcript, "reply": reply, "conversation_id": cid, "audio_wav_base64": audio_b64}


@app.post("/browser/kill", dependencies=[Depends(auth)])
async def browser_kill():
    """KILL SWITCH for the read-only browser: tear the context down instantly (no orphan chromium).
    Authed; safe to call when nothing is open. The HUD will wire a button to this later."""
    from app import browser
    res = await browser.close_browser()
    return res


# --------------- Screen-control FREEZE / panic stop ---------------
# The human panic switch for the (future) screen-control "hands". It trips the SAME freeze the
# Phase-1 policy gate already enforces: screen_policy.is_frozen() -> check_can_act() returns
# DENY("frozen"). Single source of truth = the sentinel file data/screen_freeze.flag, so the HUD
# button, the OS hotkey, and a bare `touch` of the file are all equivalent panic paths. Clearing is
# an EXPLICIT human re-arm ONLY (POST /screen/unfreeze) — it never auto-clears. No acting primitive
# exists yet; this stop is deliberately built BEFORE one ever is.

class ScreenFreezeIn(BaseModel):
    reason: str | None = None


def _screen_freeze_state() -> dict:
    """{frozen, reason, since}. `frozen` is the gate's authoritative (fail-closed) is_frozen();
    reason/since are parsed from the sentinel for display only. Never raises."""
    frozen = screen_policy.is_frozen()
    reason, since = "", ""
    if frozen:
        try:
            raw = screen_policy._FREEZE_FLAG.read_text(encoding="utf-8").strip()
            if raw:
                first = raw.splitlines()[0]
                since, _, reason = first.partition("\t")
        except Exception:
            pass                      # frozen stands even if the detail read fails (fail-closed)
    return {"frozen": frozen, "reason": reason, "since": since}


@app.post("/screen/freeze", dependencies=[Depends(auth)])
async def screen_freeze_set(inp: ScreenFreezeIn):
    """PANIC STOP — trip the screen-control freeze. Halts ALL (future) acting at the gate.
    Idempotent; safe to call repeatedly. Clearing requires the explicit POST /screen/unfreeze."""
    screen_policy.set_freeze((inp.reason or "HUD panic button").strip()[:200])
    print(f"[screen] FREEZE set via API ({(inp.reason or 'HUD panic button')[:60]})", file=sys.stderr)
    return _screen_freeze_state()


@app.post("/screen/unfreeze", dependencies=[Depends(auth)])
async def screen_freeze_clear():
    """Explicit human RE-ARM — the ONLY thing that clears the freeze. Never auto-invoked."""
    screen_policy.clear_freeze()
    print("[screen] freeze CLEARED via API (explicit re-arm)", file=sys.stderr)
    return _screen_freeze_state()


@app.get("/screen/freeze", dependencies=[Depends(auth)])
async def screen_freeze_get():
    """Current freeze state: {frozen, reason, since, hotkey}."""
    return _screen_freeze_state()


# --------------- Screen-control PHASE 1: the gated acting harness (type_into) ---------------
# A CONTROLLED harness for the first acting primitive — NOT wired into /chat NL routing. Every action
# goes propose -> human approve -> re-validate (via app/screen_policy, the gate) -> execute on a FRESH
# owns-nothing Playwright context. type_into is the ONLY primitive this phase (no click/submit/
# navigate). The gate enforces brain-floor + freeze + forbidden; this layer never reimplements them.

class ScreenProposeIn(BaseModel):
    url: str
    field: str                       # SEMANTIC locator name (label / placeholder / textbox role name)
    value: str
    rung: str | None = None          # deciding brain rung; defaults to groq (smart-brain). Gate checks it.


@app.post("/screen/propose", dependencies=[Depends(auth)])
async def screen_propose(inp: ScreenProposeIn):
    """Propose a type_into. The gate decides: DENY (stop) or CONFIRM_REQUIRED (staged, awaiting
    approve). NOTHING executes here — watch-mode."""
    from app import screen_act
    intent = {"verb": "type_into", "target": {"url": inp.url, "field": inp.field}, "value": inp.value}
    return await screen_act.propose_action(intent, inp.rung or "groq")


@app.post("/screen/approve", dependencies=[Depends(auth)])
async def screen_approve():
    """Explicit human YES — re-validate through the gate, then execute the staged type_into on the
    owns-nothing context with verify-after-act. One-shot."""
    from app import screen_act
    return await screen_act.approve_action()


@app.post("/screen/reject", dependencies=[Depends(auth)])
async def screen_reject():
    """Explicit human NO — discard the staged action. Nothing is typed."""
    from app import screen_act
    return await screen_act.reject_action()


@app.post("/screen/kill", dependencies=[Depends(auth)])
async def screen_kill():
    """In-turn KILL — abort any pending/in-flight action and tear down the owns-nothing context."""
    from app import screen_act
    return await screen_act.kill()


@app.get("/screen/hands", dependencies=[Depends(auth)])
async def screen_hands(n: int = 40):
    """The {type:"hands"} trace — recent propose/execute/result/abort events (value always redacted)."""
    from app import screen_act
    return {"events": screen_act.hands_events(n), "pending": screen_act.pending_summary()}


# --------------- WebSocket streaming (Phase 1) ---------------
# Browsers cannot set headers on WebSocket connects, so the Bearer token rides in the FIRST
# message frame — never the URL (URLs land in logs). Nothing is processed before the token
# verifies; bad/missing token closes with 1008 (policy violation). Same token, same boundary,
# same tailnet-only bind as the REST routes.

async def _ws_handshake(ws: WebSocket) -> dict | None:
    """Accept, read the first frame, verify the token. Returns the first frame on success;
    closes 1008 and returns None otherwise."""
    await ws.accept()
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close(code=1008)
        return None
    token = first.get("token") or ""
    if not _TOKEN or not isinstance(token, str) or not secrets.compare_digest(token, _TOKEN):
        await ws.close(code=1008)
        return None
    await ws.send_json({"type": "ready"})
    return first


def _ws_sender(ws: WebSocket):
    """Serialized frame sender — the audio pipeline task and the stream loop both send; the lock
    keeps frames whole and ordered on the wire."""
    lock = asyncio.Lock()
    async def send(frame: dict):
        async with lock:
            await ws.send_json(frame)
    return send


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    first = await _ws_handshake(ws)
    if first is None:
        return
    cid = first.get("conversation_id") or str(uuid.uuid4())
    want_audio = bool(first.get("audio", False))
    send = _ws_sender(ws)
    try:
        while True:
            msg = await ws.receive_json()
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            window = _windows.setdefault(cid, [])
            try:
                reply = await stream_reply(USER, text, window, send, voice=want_audio,
                                           conversation_id=cid)
            except Exception as e:   # observability #1: capture a streamed-turn failure, then re-raise
                errorlog.log_error("stream_reply", e, text)
                raise
            _advance_window(cid, text, reply)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    first = await _ws_handshake(ws)
    if first is None:
        return
    cid = first.get("conversation_id") or str(uuid.uuid4())
    send = _ws_sender(ws)
    import app.voice as v   # blocks on the warm thread's import lock if mid-warm, like REST
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data:
                continue   # ignore stray text frames between utterances
            tf = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
            try:
                tf.write(data)
                tf.close()
                clip = await asyncio.to_thread(v.transcribe_clip, tf.name)
            finally:
                os.unlink(tf.name)
            # Silent drop (no transcript bubble, no spoken nudge, NO turn / memory, window untouched)
            # when Silero hears no speech (room noise the browser VAD let through) or the transcript
            # is junk — the client finalizes cleanly on a done/reply:"" frame (the prior no-speech path).
            if not clip["speech"]:
                print("[voice vad-gate] silero: no speech -> silent drop", file=sys.stderr)
                await send({"type": "done", "reply": ""})
                continue
            transcript = clip["text"]
            if clip["junk"]:
                print(f"[voice junk-gate] transcript={transcript!r} nsp={clip['no_speech_prob']:.2f} "
                      f"alp={clip['avg_logprob']:.2f} -> silent drop (no turn)", file=sys.stderr)
                await send({"type": "done", "reply": ""})
                continue
            await send({"type": "transcript", "text": transcript})
            window = _windows.setdefault(cid, [])
            try:
                reply = await stream_reply(USER, transcript, window, send, voice=True,
                                           conversation_id=cid)
            except Exception as e:   # observability #1: capture a streamed-turn failure, then re-raise
                errorlog.log_error("stream_reply", e, transcript)
                raise
            _advance_window(cid, transcript, reply)
    except WebSocketDisconnect:
        pass

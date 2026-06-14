"""Real spend telemetry + the in-code Claude spend guard.

GROQ: today's token usage (a wrapper records every non-streamed completion; chat_stream adds the
streamed turns). CLAUDE: today's USD cost of every Agent-SDK call (consult/build/browse/selfmod),
recorded by app/agent.py. Both persist per local date in data/usage.json so the numbers survive a
restart and reflect the real day.

The Claude figures back a HARD, in-code daily cap (CLAUDE_DAILY_CAP_USD): claude_blocked_reason()
is checked at EVERY Claude entry point in app/agent.py BEFORE any SDK call, so spend is bounded by
Nervice's own code — independent of any Anthropic-account billing toggle. Never raises: telemetry
and the guard must not break a turn.
"""
import os
import sys
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Chicago")
_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
_FILE = _DIR / "usage.json"
_data: dict = {}

# HARD daily ceiling on Claude / Agent-SDK spend, enforced in code (NOT the Anthropic toggle).
# Default $5.00; override for testing with NERVICE_CLAUDE_DAILY_CAP_USD. Once today's recorded
# Claude spend reaches this, every Claude path refuses with BUDGET_MSG until tomorrow (local date).
try:
    CLAUDE_DAILY_CAP_USD = float(os.environ.get("NERVICE_CLAUDE_DAILY_CAP_USD") or 5.00)
except (TypeError, ValueError):
    CLAUDE_DAILY_CAP_USD = 5.00

BUDGET_MSG = ("I've hit today's Claude budget, so I'm staying on the free brains for the rest of "
              "the day — it resets tomorrow.")


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _load() -> None:
    global _data
    try:
        _data = json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        _data = {}


def _flush() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _FILE.write_text(json.dumps(_data), encoding="utf-8")
    except Exception as e:
        print(f"[usage] flush failed: {repr(e)[:80]}", file=sys.stderr)


def add_groq(usage) -> None:
    """Record a Groq completion's token usage. `usage` is the SDK usage object (or None). Tolerant of
    shape (total_tokens, or prompt+completion). Never raises."""
    try:
        if usage is None:
            return
        n = getattr(usage, "total_tokens", None)
        if n is None:
            n = (getattr(usage, "prompt_tokens", 0) or 0) + (getattr(usage, "completion_tokens", 0) or 0)
        n = int(n or 0)
        if n <= 0:
            return
        day = _data.setdefault(_today(), {})
        day["groq_tokens"] = int(day.get("groq_tokens", 0)) + n
        _flush()
    except Exception:
        pass


def today_groq_tokens() -> int:
    """Today's recorded Groq token total (0 if none yet)."""
    try:
        return int(_data.get(_today(), {}).get("groq_tokens", 0))
    except Exception:
        return 0


def add_claude(cost_usd) -> None:
    """Record the USD cost of one Claude / Agent-SDK call (consult/build/browse/selfmod), persisted
    per day next to the Groq counts. `cost_usd` may be None/0 (no-op). Never raises."""
    try:
        c = float(cost_usd or 0.0)
        if c <= 0:
            return
        day = _data.setdefault(_today(), {})
        day["claude_usd"] = round(float(day.get("claude_usd", 0.0)) + c, 6)
        _flush()
    except Exception:
        pass


def today_claude_usd() -> float:
    """Today's recorded Claude spend in USD (0.0 if none yet)."""
    try:
        return float(_data.get(_today(), {}).get("claude_usd", 0.0))
    except Exception:
        return 0.0


def claude_blocked_reason() -> str | None:
    """The shared, in-code Claude gate. Returns None when a Claude call is allowed, else the honest
    user-facing message to show instead. Checked at EVERY Claude entry point in app/agent.py, before
    any SDK call — so it cannot be bypassed by any route, and it holds regardless of Anthropic's own
    billing toggle. Never raises (a telemetry hiccup must not wedge a turn)."""
    try:
        if today_claude_usd() >= CLAUDE_DAILY_CAP_USD:
            return BUDGET_MSG
    except Exception:
        pass
    return None


def install_groq_meter(client) -> None:
    """Wrap a Groq client's chat.completions.create so every NON-streamed call records its usage
    automatically — one hook covers the tool loop, grounded synthesis, router/memory JSON, and the
    orchestrator. Streamed calls (chat_stream) are metered there instead (their usage arrives in a
    final chunk). Idempotent — re-installing is a no-op."""
    try:
        comp = client.chat.completions
        if getattr(comp.create, "_nervice_metered", False):
            return
        orig = comp.create

        async def metered(*a, **k):
            resp = await orig(*a, **k)
            if not k.get("stream"):
                add_groq(getattr(resp, "usage", None))
            return resp

        metered._nervice_metered = True
        comp.create = metered
    except Exception as e:
        print(f"[usage] meter install failed: {repr(e)[:80]}", file=sys.stderr)


_load()

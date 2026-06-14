"""Real token telemetry — today's GROQ token usage, persisted per day.

Lean by design: a single wrapper (install_groq_meter) records the `usage` Groq returns on every
non-streamed completion, and chat_stream adds the streamed turns' usage explicitly. Counts go to
data/usage.json keyed by local date, so the number survives a restart and reflects the real day.
Never raises — telemetry must not break a turn. Local Ollama and Claude are not counted here (Ollama
is free/local; Claude has its own cost in agent.last_run).
"""
import sys
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Chicago")
_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "usage.json"
_data: dict = {}


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
        _FILE.parent.mkdir(parents=True, exist_ok=True)
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
        d = _today()
        day = _data.setdefault(d, {"groq_tokens": 0})
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

"""ACTIONS feed — REAL audit, merged from logs/computer_actions.log + logs/browser_actions.log.

Pure parsing (no LLM) shared by GET /actions/feed (the ACTIONS panel) and the brain's grounded
recall ("what have you done"). Two on-disk formats, both naive local-time ISO timestamps:
  computer_actions.log : TAB-separated   ts \t ACTION \t target \t [args...] \t outcome
  browser_actions.log  : space + arrow   ts action target... -> outcome
No fabrication: an empty source just contributes fewer rows.
"""
import re
import sys
import time
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Chicago")
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_COMPUTER_LOG = _ROOT / "logs" / "computer_actions.log"
_BROWSER_LOG = _ROOT / "logs" / "browser_actions.log"


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=_TZ).timestamp()
    except Exception:
        return 0.0


def _ago(now: float, ts: float) -> str:
    s = max(0, int(now - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _tail(path: pathlib.Path, n: int = 120) -> list[str]:
    try:
        return [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-n:]
    except Exception:
        return []


def _parse_computer(ln: str) -> dict | None:
    f = ln.split("\t")
    if len(f) < 2:
        return None
    ts, action = f[0], f[1]
    target = f[2] if len(f) >= 3 else ""
    outcome = f[-1] if len(f) >= 4 else ""
    return {"epoch": _epoch(ts), "ts": ts, "source": "computer", "action": action,
            "target": target, "outcome": outcome}


def _parse_browser(ln: str) -> dict | None:
    # ts action target... -> outcome
    parts = ln.split(" ", 2)
    if len(parts) < 2:
        return None
    ts = parts[0]
    action = parts[1]
    rest = parts[2] if len(parts) >= 3 else ""
    target, sep, outcome = rest.partition(" -> ")
    return {"epoch": _epoch(ts), "ts": ts, "source": "browser", "action": action,
            "target": target.strip(), "outcome": outcome.strip()}


def read_feed(limit: int = 40) -> list[dict]:
    """Merged, reverse-chron action audit. Each row: {ts, ago, source, action, target, outcome}."""
    rows: list[dict] = []
    for ln in _tail(_COMPUTER_LOG):
        r = _parse_computer(ln)
        if r:
            rows.append(r)
    for ln in _tail(_BROWSER_LOG):
        r = _parse_browser(ln)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: r["epoch"], reverse=True)
    now = time.time()
    out = []
    for r in rows[:limit]:
        out.append({"ts": r["ts"], "ago": _ago(now, r["epoch"]), "source": r["source"],
                    "action": r["action"], "target": r["target"][:120], "outcome": r["outcome"][:160]})
    return out


async def summarize(user_message: str, voice_mode: bool = False) -> str:
    """Grounded recall: a SHORT, honest summary of what Nervice has actually DONE, from the real
    action audit ONLY. Free rung (Groq -> Ollama), NEVER Claude. Sets current_rung for telemetry."""
    from app.agent import current_rung
    from app.orchestrator import _ask_free   # the free-only helper (Groq -> Ollama -> none)
    feed = read_feed(limit=15)
    if not feed:
        current_rung.set("direct")
        return "I don't have any actions logged yet — nothing I've done has hit the audit log."
    lines = "\n".join(f"{r['ago']}: [{r['source']}] {r['action']} {r['target']} -> {r['outcome']}"
                      for r in feed)
    system = ("You are Nervice telling Nate what you have actually DONE recently, using ONLY the real "
              "audit log below. Keep it SHORT and conversational (1-3 sentences) — group similar "
              "actions, lead with the most recent. NEVER invent an action that isn't in the log. If "
              "there's nothing notable, say so plainly. No lists, no markdown.")
    ans, rung = await _ask_free(system, f"RECENT ACTIONS (newest first):\n{lines}\n\nNate asked: {user_message}",
                                want_json=False, num_predict=200)
    if rung == "none":
        return "I can't summarize my recent actions right now — the fast model's rate-limited and my local backup's down."
    return ans

"""Headless Claude Code usage — a SEPARATE, ADDITIVE ledger + a FAIL-CLOSED budget gate.

This is NOT the dollar spend guard. The headless `claude -p` subprocess (app/cc_headless.py) bills the
SUBSCRIPTION (OAuth via ~/.claude-nervice), not the metered API — so usage.py's $5/day dollar cap
correctly reads zero for it. The real bound on subscription headless work is RUN/STEP COUNT (quota),
with reported cost as a secondary ceiling for the days the CLI does return a number.

What this module does:
  - Records every headless run to data/headless_usage.jsonl (append-only, one JSON object per line).
  - Gates the NEXT run on a conservative daily RUN-COUNT cap AND a daily reported-COST ceiling,
    checked BEFORE each spawn in run_headless_step.
  - Parses `claude -p --output-format json` for cost/usage and detects a subscription rate/usage limit
    so a FUTURE watcher can pause (no loop here — that is a later build).

What it NEVER touches: usage.py's $5 cap / free_only / add_claude, safety.py, selfmod.py, SAFETY_FLOOR,
agent.py's key scrub. It adds a separate bound; it does not weaken or modify the dollar guard.

FAIL-CLOSED by design (unlike usage.py's never-raise telemetry): a MISSING ledger is the normal empty
state (0 runs, gate open); a PRESENT-but-unreadable/corrupt ledger PAUSES headless work (gate refuses)
until a human looks. A tampered or half-written ledger must stop runs, not be silently skipped.
"""
import os
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("America/Chicago")                 # mirror usage.py's local-date convention
_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
LEDGER_PATH = _DIR / "headless_usage.jsonl"       # append-only; SEPARATE from data/usage.json


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# ---- CONSERVATIVE daily bounds (clearly labeled). Overridable via env for cheap gate testing. ----
DAILY_RUN_CAP = _int_env("NERVICE_HEADLESS_RUN_CAP", 20)                  # max headless spawns / local day
DAILY_COST_CEILING_USD = _float_env("NERVICE_HEADLESS_COST_CEILING_USD", 2.00)  # reported-cost ceiling / day

# Substrings that signal a SUBSCRIPTION rate/usage limit in claude -p output (paired with non-zero exit).
_RATE_LIMIT_TOKENS = ("rate limit", "rate_limit", "usage limit", "usage_limit",
                      "too many requests", "overloaded", "limit reached",
                      "limit exceeded", "exceeded your usage", "quota")


class LedgerUnreadable(Exception):
    """The ledger exists but cannot be read/parsed — the gate fails CLOSED (pauses) on this."""


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _read_all(path=None) -> list:
    """Every ledger record (list of dicts). Missing file -> [] (normal empty). Present-but-
    unreadable, or ANY corrupt/non-object line -> raise LedgerUnreadable (fail-closed on purpose:
    a tampered or half-written ledger must PAUSE headless work, not be silently skipped)."""
    p = pathlib.Path(path) if path else LEDGER_PATH
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as e:
        raise LedgerUnreadable(f"cannot read {p.name}: {type(e).__name__}") from e
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception as e:
            raise LedgerUnreadable(f"corrupt line in {p.name}: {type(e).__name__}") from e
        if not isinstance(rec, dict):
            raise LedgerUnreadable(f"non-object line in {p.name}")
        out.append(rec)
    return out


def _sum_cost(recs) -> float:
    total = 0.0
    for r in recs:
        try:
            total += float(r.get("reported_cost") or 0.0)
        except (TypeError, ValueError):
            pass
    return round(total, 6)


def today_usage(path=None) -> dict:
    """Today's aggregates: {count, reported_cost}. Raises LedgerUnreadable on a corrupt ledger."""
    day = _today()
    today = [r for r in _read_all(path) if r.get("date") == day]
    return {"count": len(today), "reported_cost": _sum_cost(today)}


def record_run(entry: dict, path=None) -> bool:
    """Append ONE run record (additive only). Stamps ts/date if absent. Returns True on write, False
    on failure. A failed record cannot un-run the step, but the NEXT gate_check fails CLOSED if the
    ledger is now unreadable — so a broken ledger still halts further runs."""
    p = pathlib.Path(path) if path else LEDGER_PATH
    rec = dict(entry or {})
    rec.setdefault("ts", datetime.now(_TZ).isoformat(timespec="seconds"))
    rec.setdefault("date", _today())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return True
    except Exception:
        return False


def gate_check(path=None, run_cap=None, cost_ceiling=None) -> dict:
    """The pre-spawn budget gate. Returns {allowed, reason, count, cap, reported_cost, ceiling}.
    FAIL-CLOSED: a corrupt/unreadable ledger -> allowed False (paused). A missing ledger -> 0 runs,
    allowed. Refuses when today's run-count >= cap OR today's reported-cost >= ceiling. No side
    effects. run_cap / cost_ceiling override the module defaults (used to trip the gate cheaply)."""
    cap = DAILY_RUN_CAP if run_cap is None else int(run_cap)
    ceil = DAILY_COST_CEILING_USD if cost_ceiling is None else float(cost_ceiling)
    try:
        u = today_usage(path)
    except LedgerUnreadable as e:
        return {"allowed": False, "reason": f"PAUSED (fail-closed): headless ledger unreadable - {e}",
                "count": None, "cap": cap, "reported_cost": None, "ceiling": ceil}
    count, cost = u["count"], u["reported_cost"]
    if count >= cap:
        return {"allowed": False, "reason": "headless daily budget reached",
                "count": count, "cap": cap, "reported_cost": cost, "ceiling": ceil}
    if cost >= ceil:
        return {"allowed": False, "reason": "headless daily cost ceiling reached",
                "count": count, "cap": cap, "reported_cost": cost, "ceiling": ceil}
    return {"allowed": True, "reason": None, "count": count, "cap": cap,
            "reported_cost": cost, "ceiling": ceil}


def parse_claude_result(stdout: str) -> dict:
    """Extract cost/usage from `claude -p --output-format json` stdout. The result is a single JSON
    object; tolerate extra/leading lines by trying the whole blob then the last `{...}` line. Returns
    {reported_cost, input_tokens, output_tokens, turns, duration_ms, is_error, subtype, session_id,
    parsed}. parsed=False if no JSON object was found. reported_cost defaults 0.0 (subscription often
    reports no/zero cost — then run-count is the real bound)."""
    meta = {"reported_cost": 0.0, "input_tokens": None, "output_tokens": None, "turns": None,
            "duration_ms": None, "is_error": None, "subtype": None, "session_id": None, "parsed": False}
    if not stdout:
        return meta
    s = stdout.strip()
    obj = None
    candidates = [s] + [ln.strip() for ln in reversed(s.splitlines()) if ln.strip().startswith("{")]
    for cand in candidates:
        try:
            o = json.loads(cand)
        except Exception:
            continue
        if isinstance(o, dict):
            obj = o
            break
    if obj is None:
        return meta
    meta["parsed"] = True
    for k in ("total_cost_usd", "cost_usd", "total_cost"):
        if obj.get(k) is not None:
            try:
                meta["reported_cost"] = float(obj.get(k) or 0.0)
            except (TypeError, ValueError):
                pass
            break
    usage = obj.get("usage")
    if isinstance(usage, dict):
        meta["input_tokens"] = usage.get("input_tokens")
        meta["output_tokens"] = usage.get("output_tokens")
    meta["turns"] = obj.get("num_turns")
    meta["duration_ms"] = obj.get("duration_ms")
    meta["is_error"] = obj.get("is_error")
    meta["subtype"] = obj.get("subtype")
    meta["session_id"] = obj.get("session_id")
    return meta


def detect_rate_limit(rc, stdout: str, stderr: str, meta=None) -> bool:
    """True when claude -p signals a SUBSCRIPTION rate/usage limit: a non-zero exit AND a limit token
    in stdout/stderr, OR a parsed result flagged is_error with a limit signal/subtype. Conservative —
    a plain non-zero exit (an ordinary task failure with no limit signal) is NOT treated as a limit,
    so a future watcher only pauses on a real quota signal."""
    blob = f"{stdout or ''}\n{stderr or ''}".lower()
    signal = any(tok in blob for tok in _RATE_LIMIT_TOKENS)
    if (rc not in (0, None)) and signal:
        return True
    if meta and meta.get("is_error"):
        sub = (meta.get("subtype") or "").lower()
        if signal or "limit" in sub:
            return True
    return False


def usage_view(path=None) -> dict:
    """Read-only view for GET /loop/headless-usage: today's run-count vs cap, reported-cost vs ceiling,
    whether the gate is open right now, and the last few runs (newest first). FAIL-CLOSED: an unreadable
    ledger reports gate_open False + ledger_ok False rather than a falsely-open gate."""
    cap, ceil = DAILY_RUN_CAP, DAILY_COST_CEILING_USD
    try:
        recs = _read_all(path)
    except LedgerUnreadable as e:
        return {"today_count": None, "cap": cap, "today_reported_cost": None, "ceiling": ceil,
                "gate_open": False, "ledger_ok": False, "error": str(e), "last_runs": []}
    day = _today()
    today = [r for r in recs if r.get("date") == day]
    cost = _sum_cost(today)
    gate_open = (len(today) < cap) and (cost < ceil)
    return {"today_count": len(today), "cap": cap, "today_reported_cost": cost, "ceiling": ceil,
            "gate_open": gate_open, "ledger_ok": True, "last_runs": recs[-10:][::-1]}

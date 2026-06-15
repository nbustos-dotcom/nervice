"""Central error log (observability PART 1).

One JSON line per notable/handled failure across the turn paths (recall, the route executors,
respond / stream_reply), captured WITHOUT swallowing the error and WITHOUT crashing the turn — every
write is best-effort and `log_error` never raises. Secrets are scrubbed from any logged context: the
exact VALUES of the agent.py `_SCRUB_KEYS` env vars (+ the API token) plus any token-shaped substring,
so a pasted/typed credential can't leak. Read side: `read_recent(n)` powers GET /errors/recent and
`count_since(iso)` feeds GET /stats/history's error count.

Lightweight by design: stdlib only, no framework, no per-turn writes unless something actually failed.
"""
import os
import re
import sys
import json
import pathlib
import datetime
import traceback

_LOG = pathlib.Path(__file__).resolve().parent.parent / "logs" / "errors.log"
_MAX_BYTES = 2_000_000            # self-trim guard so the log can't grow unbounded (no handler needed)
_TAIL_BYTES = 200_000            # bounded read for the read side — safe on a large/rotating file

# Reuse agent.py's scrub discipline (the env vars whose VALUES must never hit a log) + the API token.
_SECRET_ENV = ("GROQ_API_KEY", "DATABASE_URL", "DATABASE_URL_MIGRATIONS", "CLAUDE_CODE_OAUTH_TOKEN",
               "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "NERVICE_API_TOKEN")
# Token-SHAPED substrings (a credential pasted/typed into a message that isn't an env value): auth
# headers, common provider key prefixes, and any long high-entropy run. Over-redacts rather than leak.
_TOKENISH = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"
    r"|\b(?:sk|gsk|pk|rk|api|key|tok|ghp|xox[baprs])[-_][A-Za-z0-9]{10,}"
    r"|\b[A-Za-z0-9_\-]{32,}\b")


def scrub(text: str) -> str:
    """Redact secrets from logged text: exact env-var VALUES first (-> [scrubbed:NAME]), then any
    token-shaped substring (-> [scrubbed]). Conservative: prefers over-redaction to a leak."""
    if not text:
        return ""
    s = str(text)
    for name in _SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) >= 6 and val in s:
            s = s.replace(val, f"[scrubbed:{name}]")
    return _TOKENISH.sub("[scrubbed]", s)


def log_error(where: str, exc, context: str = "", max_context: int = 300) -> None:
    """Append one scrubbed JSON line for a notable failure. NEVER raises, NEVER swallows (the caller's
    own handling/degrade is unchanged — this only records).
      where    : route/function, e.g. 'recall:embed', 'route:canvas', 'respond', 'stream_reply'.
      exc      : an Exception (type + message + short traceback captured) or a plain string.
      context  : minimal context (the user message is OK) — scrubbed and length-capped.
    Tags an Exception with `_nervice_logged` so an OUTER handler won't log the same error twice."""
    try:
        if isinstance(exc, BaseException):
            if getattr(exc, "_nervice_logged", False):
                return                                  # already captured deeper in the stack
            try:
                exc._nervice_logged = True
            except Exception:
                pass
            etype = type(exc).__name__
            emsg = scrub(str(exc))[:300]
            tb = scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-900:]
        else:
            etype, emsg, tb = "Error", scrub(str(exc))[:300], ""
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "where": str(where)[:60],
            "type": etype,
            "message": emsg,
            "context": scrub(context)[:max_context],
        }
        if tb:
            entry["traceback"] = tb
        # Surface live on stderr too (consistent with the [turn]/[recall] lines) — honest, not hidden.
        print(f"[error] {entry['where']}: {etype}: {emsg}", file=sys.stderr)
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim_if_huge()
    except Exception:
        pass                                            # logging must never crash a turn


def _trim_if_huge() -> None:
    """Keep errors.log bounded without a rotation handler: past _MAX_BYTES, keep the recent tail."""
    try:
        if _LOG.stat().st_size <= _MAX_BYTES:
            return
        data = _LOG.read_bytes()[-(_MAX_BYTES // 2):]
        nl = data.find(b"\n")                           # drop the partial first line
        if nl != -1:
            data = data[nl + 1:]
        _LOG.write_bytes(data)
    except Exception:
        pass


def _tail_lines() -> list:
    """The last _TAIL_BYTES of the log as decoded lines (bounded read). Never raises."""
    try:
        with open(_LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - _TAIL_BYTES))
            return f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def read_recent(n: int = 20) -> list:
    """The last n entries, NEWEST FIRST. Tail-bounded read; malformed lines skipped. Never raises."""
    n = max(1, min(int(n), 200))
    out = []
    for line in reversed(_tail_lines()):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue                                    # malformed line skipped gracefully
        if len(out) >= n:
            break
    return out


def count_since(cutoff_iso: str) -> int:
    """How many error entries have ts >= cutoff_iso. The ts field is ISO-8601 (timespec=seconds), so
    a lexicographic string compare is a correct time compare. Tail-bounded; never raises."""
    n = 0
    for line in _tail_lines():
        line = line.strip()
        if not line:
            continue
        try:
            if json.loads(line).get("ts", "") >= cutoff_iso:
                n += 1
        except Exception:
            continue
    return n

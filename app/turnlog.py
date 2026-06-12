"""One structured line per turn: which rung answered + how long, to stderr (live tail) and a
rotating logs/turns.log. Purpose: instantly see whether normal conversation is escalating to
Claude (Groq capped or misroute) vs slow elsewhere. Lightweight — one log call per turn."""
import sys
import pathlib
import logging
from logging.handlers import RotatingFileHandler

_logger = logging.getLogger("nervice.turns")
_logger.setLevel(logging.INFO)
_logger.propagate = False
try:                                  # import must never fail just because logs/ is unwritable
    _LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not _logger.handlers:
        _h = RotatingFileHandler(_LOG_DIR / "turns.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(asctime)s\t%(message)s"))
        _logger.addHandler(_h)
except Exception:
    pass                              # degrade to stderr-only; the [turn] line still flows


def log_turn(route: str, rung: str, total_s: float, path: str, extra: str = "") -> None:
    """route = router decision; rung = the most expensive rung this turn touched (groq / claude-pro
    / claude-max / exhausted / control) — i.e. whether the turn escalated; total_s = wall time;
    path = rest | ws. `extra` appends optional tab-separated context (e.g. reason=confusion);
    the /ladder and /last-turn parsers tolerate trailing fields. Never raises."""
    line = f"route={route}\trung={rung}\t{total_s:.2f}s\tpath={path}"
    if extra:
        line += f"\t{extra}"
    print(f"[turn] {line}", file=sys.stderr)
    try:
        _logger.info(line)
    except Exception:
        pass


def log_event(tag: str) -> None:
    """A non-turn marker line in the same log (e.g. GUARD-TRIP). Deliberately a different shape
    from turn lines so the turns.log parsers (which match route=...) skip it. Never raises."""
    print(f"[turn] {tag}", file=sys.stderr)
    try:
        _logger.info(tag)
    except Exception:
        pass

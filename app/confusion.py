"""Fix 2.4: Claude-on-confusion — deterministic pre-route confusion detection.

Evidence: during the play-some-music spiral the user repeated/corrected four times and the
Claude tier never fired. Three signals, all deterministic (no LLM in the detector):
  (a) the utterance is a normalized near-duplicate of one of the user's last 3 utterances
  (b) it opens with a correction (no, / that's not / I said / wrong / you didn't)
  (c) the PREVIOUS turn ended exhausted or with a GUARD-TRIP (the 4B honesty guard fired)

Level 1 (first signal): stay on the current rung, inject a one-line hint into the system
prompt — address the repetition/correction directly.
Level 2 (signals on CONSECUTIVE turns): route the turn to consult_claude regardless of topic,
budgeted at MAX_PER_HOUR escalations per rolling hour (beyond that, stay level 1 — Claude is
scarce). Escalated turns log reason=confusion in turns.log.

Single-user module state, in-process — same pattern as computer._pending."""
import re
import sys
import time

_HIST: dict[str, list] = {}           # user -> last 3 normalized routed utterances
_PREV_SIGNAL: dict[str, bool] = {}    # user -> the previous routed turn raised a signal
_PREV_BAD_END: dict[str, bool] = {}   # user -> previous turn ended exhausted / guard-tripped
_escalations: list[float] = []        # rolling-hour escalation budget
MAX_PER_HOUR = 4

_CORRECTION_RE = re.compile(
    r"^\s*(no[,.!\s]|that'?s\s+not\b|i\s+said\b|wrong\b|you\s+didn'?t\b)", re.I)

HINT = ("\n\nNOTE: the user appears to be repeating themselves or correcting you — address "
        "that directly and briefly; do not repeat your previous answer.")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (t or "").lower()).strip()


def _near_dup(a: str, b: str) -> bool:
    """Exact normalized match, >=0.7 Jaccard token overlap, or full containment of a >=3-token
    ask — 'play some music' / 'play some music.' / 'jarvis play some music' all read as the
    same ask (the real spiral's variants score 0.75). False positives only cost a hint;
    escalation needs CONSECUTIVE signals."""
    if not a or not b:
        return False
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 3 and small <= big:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.7


def check(user_id: str, text: str) -> str | None:
    """Call once per ROUTED turn (after the deterministic gates, before execution).
    Returns None, 'hint' (level 1), or 'escalate' (level 2, budget permitting)."""
    n = _norm(text)
    hist = _HIST.setdefault(user_id, [])
    signal = (_PREV_BAD_END.get(user_id, False)
              or bool(_CORRECTION_RE.search(text or ""))
              or any(_near_dup(n, h) for h in hist))
    hist.append(n)
    del hist[:-3]
    prev = _PREV_SIGNAL.get(user_id, False)
    _PREV_SIGNAL[user_id] = signal
    if not signal:
        return None
    if prev:                                   # consecutive signal turns -> escalate if budget
        now = time.time()
        global _escalations
        _escalations = [t for t in _escalations if now - t < 3600]
        if len(_escalations) < MAX_PER_HOUR:
            _escalations.append(now)
            print(f"[confusion] consecutive signal -> consult_claude "
                  f"({len(_escalations)}/{MAX_PER_HOUR} this hour)", file=sys.stderr)
            return "escalate"
        print("[confusion] escalation budget spent — staying level 1", file=sys.stderr)
    return "hint"


def note_turn_end(user_id: str, rung: str, guard_tripped: bool) -> None:
    """Record how the turn ended — an exhausted ladder or a 4B guard trip makes the NEXT
    utterance a confusion signal even if it isn't a repeat or correction."""
    _PREV_BAD_END[user_id] = (rung == "exhausted") or bool(guard_tripped)

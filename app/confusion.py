"""Deterministic pre-route confusion detection — FREE by construction (it never auto-spends).

Evidence: during the play-some-music spiral the user repeated/corrected four times. Three signals,
all deterministic (no LLM in the detector):
  (a) the utterance is a normalized near-duplicate of one of the user's last 3 utterances
  (b) it opens with a correction (no, / that's not / I said / wrong / you didn't)
  (c) the PREVIOUS turn ended exhausted or with a GUARD-TRIP (the 4B honesty guard fired)

On a signal, Nervice injects a one-line HINT into the system prompt — address the repetition/
correction directly, on whatever rung is already handling the turn. That is the whole mechanism.

WHY THERE'S NO 'ESCALATE' (precaution #4): an earlier version escalated a repeat/correction
straight to PAID Claude (consult_claude). It fired MOST exactly when Groq was capped — the costly
state — making user frustration an auto-spend trigger. That path is removed: confusion is now a
free, in-prompt nudge and can NEVER, by itself, cause a Claude call. (The global spend guard,
app/usage.claude_blocked_reason enforced in app/agent.py, governs every Claude path regardless, so
the cap and the free-only switch are respected vacuously here — confusion simply never reaches it.)

Single-user module state, in-process — same pattern as computer._pending."""
import re

_HIST: dict[str, list] = {}           # user -> last 3 normalized routed utterances
_PREV_BAD_END: dict[str, bool] = {}   # user -> previous turn ended exhausted / guard-tripped

_CORRECTION_RE = re.compile(
    r"^\s*(no[,.!\s]|that'?s\s+not\b|i\s+said\b|wrong\b|you\s+didn'?t\b)", re.I)

HINT = ("\n\nNOTE: the user appears to be repeating themselves or correcting you — address "
        "that directly and briefly; do not repeat your previous answer.")


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (t or "").lower()).strip()


def _near_dup(a: str, b: str) -> bool:
    """Exact normalized match, >=0.7 Jaccard token overlap, or full containment of a >=3-token
    ask — 'play some music' / 'play some music.' / 'jarvis play some music' all read as the same
    ask. A false positive only costs a free in-prompt hint."""
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
    """Call once per ROUTED turn (after the deterministic gates, before execution). Returns 'hint'
    when the user seems to be repeating or correcting, else None. It NEVER returns an
    escalate-to-Claude signal — confusion no longer auto-spends (precaution #4); it only asks the
    caller to inject the free HINT into the current rung's system prompt."""
    n = _norm(text)
    hist = _HIST.setdefault(user_id, [])
    signal = (_PREV_BAD_END.get(user_id, False)
              or bool(_CORRECTION_RE.search(text or ""))
              or any(_near_dup(n, h) for h in hist))
    hist.append(n)
    del hist[:-3]
    return "hint" if signal else None


def note_turn_end(user_id: str, rung: str, guard_tripped: bool) -> None:
    """Record how the turn ended — an exhausted ladder or a 4B guard trip makes the NEXT
    utterance a confusion signal even if it isn't a repeat or correction."""
    _PREV_BAD_END[user_id] = (rung == "exhausted") or bool(guard_tripped)

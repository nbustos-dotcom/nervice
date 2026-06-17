"""Screen-control POLICY ENGINE — the deterministic gate for Nervice's (future) hands on the
live desktop. DECISION LOGIC ONLY: nothing here clicks, types, focuses, or launches a browser.
It decides whether a *proposed* screen action may be ALLOW / CONFIRM_REQUIRED / DENY, and that
decision is made in CODE, never by model judgment. Phase 1 (watch-mode): ALLOW is never returned —
everything is proposed for human approval.

This mirrors the safety DNA already in the repo (see docs/SCREEN_CONTROL_ARCHITECTURE.md §2.2):
  - app/computer.py  — a CLOSED action vocabulary + deterministic SAFE/RISKY classification that
                       fail-safes to the riskier class, plus a per-decision audit log.
  - app/selfmod.py   — fail-closed allowlist + RE-VALIDATE the action from the concrete action
                       itself (never trust a caller-supplied label) before it may proceed.
  - app/errorlog.py  — the secret-scrub util reused for the audit log.

THE INVARIANTS (all enforced here, deterministically, fail-safe to the safe side):
  1. CLOSED VOCABULARY — only the enumerated verbs are expressible; anything else is FORBIDDEN.
  2. DEFAULT-DENY — unknown verb / unknown target / unparseable intent -> FORBIDDEN, never a guess.
  3. CLASS BY (verb + target) ONLY — the risk class is NEVER influenced by the `value` (the text to
     be typed). Injected instructions hiding inside `value` cannot change the class. This is the
     core prompt-injection defense and is asserted by the test suite.
  4. RE-VALIDATE AT THE GATE — check_can_act recomputes classify() from the concrete intent and
     ignores any caller-supplied class hint (selfmod's defense-in-depth).
  5. BRAIN FLOOR — only the smart brain (Groq, or Claude when Groq is capped) may drive an action;
     a decision riding the local 4B / extractive / exhausted rung is rejected before it fires.
  6. FREEZE — a sentinel file (data/screen_freeze.flag) halts all acting and survives restarts.
  7. CIRCUIT BREAKERS — max steps/session, max actions/min, and same-verb+target loop detection.
  8. AUDIT — every decision is appended to logs/screen_actions.log, secrets scrubbed, `value`
     NEVER logged raw (a [redacted] marker only).

NOT wired into chat.py / router.py / api.py. Importing it has no side effects beyond reading a
flag file's existence. /health is unaffected.
"""
import re
import sys
import time
import pathlib
import datetime

from app.errorlog import scrub   # reuse the existing secret-scrub discipline for the audit log

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_AUDIT_LOG = _ROOT / "logs" / "screen_actions.log"
_FREEZE_FLAG = _ROOT / "data" / "screen_freeze.flag"


# ===========================================================================
# 1. THE CLOSED ACTION VOCABULARY (only these verbs are expressible)
# ===========================================================================
# TRIVIAL  — reversible, no state leaves the machine.
# CONSEQUENTIAL — state-changing / hard to undo / leaves the machine.
# Anything NOT in KNOWN_VERBS is outside the vocabulary -> FORBIDDEN (default-deny).
TRIVIAL_VERBS = frozenset({"focus_window", "screenshot", "scroll", "read_element"})
CONSEQUENTIAL_VERBS = frozenset({"type_into", "submit", "click"})
KNOWN_VERBS = TRIVIAL_VERBS | CONSEQUENTIAL_VERBS

# screenshot is the only verb that legitimately needs no target; every other verb must name one,
# or the intent is treated as missing-field -> FORBIDDEN (never guess a target).
_VERBS_NEED_TARGET = KNOWN_VERBS - {"screenshot"}

# Explicit sensitive-ACTION verbs that must stay FORBIDDEN even if some future edit adds them to a
# vocabulary. Today they're already caught by default-deny (not in KNOWN_VERBS); this is belt-and-
# suspenders, and documents the boundary. (Architecture §2.2 FORBIDDEN class.)
FORBIDDEN_VERBS = frozenset({
    "delete_file", "delete", "move_file", "rename_file", "remove_file",
    "run_shell", "run", "exec", "execute", "shell", "spawn", "cmd", "powershell",
    "registry_write", "regedit", "install", "uninstall",
    "enter_password", "enter_credential", "type_password", "oauth_consent",
})


# ===========================================================================
# 2. FORBIDDEN BLOCKLISTS (named constants) — sensitive domains, targets, actions
# ===========================================================================
# Sensitive DOMAINS: banking, brokerage/crypto, payment. Substring-matched against the target
# (broad on purpose — over-blocking is the safe direction). Email-account *settings*, oauth/consent
# and password-reset are matched by pattern below (they're not single domains).
SENSITIVE_DOMAINS = frozenset({
    # banking
    "chase.com", "bankofamerica.com", "bofa.com", "wellsfargo.com", "citi.com", "citibank.com",
    "capitalone.com", "usbank.com", "pnc.com", "truist.com", "ally.com", "discover.com",
    "americanexpress.com", "amex.com", "hsbc.com", "barclays.com",
    # brokerage / crypto
    "fidelity.com", "schwab.com", "etrade.com", "vanguard.com", "robinhood.com", "tdameritrade.com",
    "merrilledge.com", "morganstanley.com", "coinbase.com", "kraken.com", "binance.com", "gemini.com",
    # payment / money movement
    "paypal.com", "venmo.com", "wise.com", "zellepay.com", "cash.app", "stripe.com",
})

# Sensitive TARGET markers (regex, applied to the target string ONLY — never to `value`). Catches the
# FORBIDDEN categories that aren't a single domain: oauth/consent, password-reset, credential entry,
# account/email settings, registry, shell/exec, file delete/move. Mirrors computer.py's _RISKY_WORDS.
_SENSITIVE_TARGET = re.compile(
    r"\boauth\b|\bconsent\b|\bauthorize\b|\bsso\b|"                                  # oauth / consent
    r"password[\s\-_]*reset|reset[\s\-_]*password|forgot[\s\-_]*password|"          # password reset
    r"\bpassword\b|\bpasswd\b|\bcredential|\bcvv\b|\bssn\b|\bpin\b|"                 # credential entry
    r"card[\s\-_]*number|\bsecret\b|\bapi[\s\-_]*key\b|"
    r"account[\s\-_]*settings|security[\s\-_]*settings|email[\s\-_]*settings|"       # account/email settings
    r"privacy[\s\-_]*settings|password[\s\-_]*reset|"
    r"\bregistry\b|\bregedit\b|"                                                     # registry
    r"\bsudo\b|powershell|command[\s\-_]*prompt|\bcmd\b|\bterminal\b|\bbash\b|"      # shell / exec
    r"\bexec\b|\bshell\b|\bscript\b|"
    r"\bdelete\b|\bremove\b|\berase\b|\bwipe\b|\bformat\b|\bmove\b|\brename\b|\boverwrite\b",  # file delete/move
    re.I)

# Elevated / admin targets are walled off by Windows UIPI and out of scope entirely (architecture §1.3).
_ELEVATED = re.compile(r"\belevated\b|\badministrator\b|\badmin\b|\buac\b|run\s+as\s+admin", re.I)


# ===========================================================================
# 3. BRAIN FLOOR — only the smart brain may drive an action
# ===========================================================================
# The deciding rung strings come from app/agent.py's `current_rung` contextvar. The smart rungs are
# "groq" and Claude — but the real escalation sets "claude-<account>" (e.g. "claude-pro"/"claude-max",
# agent.py:226), NOT a bare "claude". So accept the claude family by prefix. Everything else
# ("ollama", "extractive", "exhausted", "browse-read", "direct", "skill", "", unknown) is rejected
# by default-deny — an acting decision must ride Groq or Claude, never the local 4B.
SMART_RUNGS = frozenset({"groq", "claude"})


def _is_smart_rung(rung) -> bool:
    if not isinstance(rung, str):
        return False
    r = rung.strip().lower()
    return r in SMART_RUNGS or r.startswith("claude-")


# ===========================================================================
# 4. CIRCUIT BREAKERS (session-scoped, resettable) — architecture §2.5
# ===========================================================================
MAX_STEPS_PER_SESSION = 40
MAX_ACTIONS_PER_MIN = 20
LOOP_REPEAT_LIMIT = 3           # same verb+target more than this many times in a row -> trip
_RATE_WINDOW_SEC = 60.0


class _CircuitBreaker:
    """Deterministic limits that trip without a human (runaway-loop / agency-creep containment).
    `clock` is injectable so the rate window is testable without real waits."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.steps = 0
        self._action_times: list[float] = []
        self._last_key: tuple | None = None
        self._repeat = 0

    def record_and_check(self, verb: str, target: str) -> str | None:
        """Record one action attempt and return a trip reason, or None if all limits hold.
        Recording happens BEFORE the threshold test, so the tripping attempt itself is counted and
        the breaker stays open until reset()."""
        now = self.clock()

        # max steps / session
        self.steps += 1
        if self.steps > MAX_STEPS_PER_SESSION:
            return f"circuit-breaker: max steps per session ({MAX_STEPS_PER_SESSION}) exceeded"

        # max actions / minute (sliding window)
        self._action_times = [t for t in self._action_times if now - t < _RATE_WINDOW_SEC]
        self._action_times.append(now)
        if len(self._action_times) > MAX_ACTIONS_PER_MIN:
            return f"circuit-breaker: action rate ({MAX_ACTIONS_PER_MIN}/min) exceeded"

        # same verb+target repeated in a row
        key = (verb, target)
        if key == self._last_key:
            self._repeat += 1
        else:
            self._last_key, self._repeat = key, 1
        if self._repeat > LOOP_REPEAT_LIMIT:
            return f"circuit-breaker: loop detected (same verb+target x{self._repeat})"

        return None


_breaker = _CircuitBreaker()


def reset_session() -> None:
    """Reset the session-scoped circuit-breaker counters (new acting session / test isolation)."""
    _breaker.reset()


# ===========================================================================
# The verdict object
# ===========================================================================
class Decision:
    """The deterministic verdict for one proposed screen action."""
    def __init__(self, outcome: str, risk_class: str | None, reason: str):
        self.outcome = outcome          # 'DENY' | 'CONFIRM_REQUIRED' | 'ALLOW' (ALLOW never in Phase 1)
        self.risk_class = risk_class    # 'TRIVIAL' | 'CONSEQUENTIAL' | 'FORBIDDEN' | None
        self.reason = reason

    def __repr__(self) -> str:
        return f"Decision(outcome={self.outcome!r}, risk_class={self.risk_class!r}, reason={self.reason!r})"


# ===========================================================================
# Intent field access (deterministic, defensive — never guesses)
# ===========================================================================
def _verb(intent) -> str:
    """The proposed verb, lowercased. '' if absent/unparseable (-> FORBIDDEN, never guessed)."""
    if not isinstance(intent, dict):
        return ""
    v = intent.get("verb")
    return v.strip().lower() if isinstance(v, str) else ""


def _target(intent) -> str:
    """The proposed target, lowercased. Accepts `target_element` (architecture's name) or `target`.
    '' if absent. NOTE: this is the ONLY field besides the verb that classify() may read."""
    if not isinstance(intent, dict):
        return ""
    t = intent.get("target_element")
    if not isinstance(t, str):
        t = intent.get("target")
    return t.strip().lower() if isinstance(t, str) else ""


def _is_elevated(intent, target: str) -> bool:
    flagged = isinstance(intent, dict) and intent.get("elevated") is True
    return bool(flagged or _ELEVATED.search(target or ""))


def _is_sensitive_target(target: str) -> bool:
    if not target:
        return False
    low = target.lower()
    if any(dom in low for dom in SENSITIVE_DOMAINS):
        return True
    return bool(_SENSITIVE_TARGET.search(low))


# ===========================================================================
# classify(intent) — deterministic, fail-safe to FORBIDDEN
# ===========================================================================
def classify(intent) -> str:
    """Map a proposed intent to its risk class: 'TRIVIAL' | 'CONSEQUENTIAL' | 'FORBIDDEN'.

    Deterministic and fail-safe: anything malformed, unknown, or sensitive -> FORBIDDEN.
    CRITICAL: the class is decided by VERB + TARGET only. The `value` field (text to be typed) is
    NEVER read here — injected instructions inside `value` cannot change the class.
    """
    verb = _verb(intent)
    target = _target(intent)

    # missing/unparseable verb -> FORBIDDEN (never guess)
    if not verb:
        return "FORBIDDEN"

    # explicit sensitive-action verb, or verb outside the closed vocabulary -> FORBIDDEN (default-deny)
    if verb in FORBIDDEN_VERBS:
        return "FORBIDDEN"
    if verb not in KNOWN_VERBS:
        return "FORBIDDEN"

    # a known verb that needs a target but has none -> missing field -> FORBIDDEN (never guess)
    if verb in _VERBS_NEED_TARGET and not target:
        return "FORBIDDEN"

    # elevated/admin target, or a sensitive domain/action target -> FORBIDDEN (blocklist wins over verb)
    if _is_elevated(intent, target):
        return "FORBIDDEN"
    if _is_sensitive_target(target):
        return "FORBIDDEN"

    # known CONSEQUENTIAL verb on a non-sensitive target -> CONSEQUENTIAL
    if verb in CONSEQUENTIAL_VERBS:
        return "CONSEQUENTIAL"

    # known TRIVIAL verb on a non-sensitive target -> TRIVIAL
    if verb in TRIVIAL_VERBS:
        return "TRIVIAL"

    # unreachable (KNOWN_VERBS is exactly TRIVIAL|CONSEQUENTIAL), but fail-safe anyway
    return "FORBIDDEN"


# ===========================================================================
# Freeze — a sentinel file that halts acting and survives restarts (§2.5)
# ===========================================================================
def is_frozen() -> bool:
    """True if the global freeze is set. Fail-closed: if the check itself errors, assume frozen
    (deny is the safe direction)."""
    try:
        return _FREEZE_FLAG.exists()
    except Exception:
        return True


def set_freeze(reason: str = "") -> None:
    """Set the global freeze. Persists across restarts. Records the reason + timestamp for forensics."""
    try:
        _FREEZE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        _FREEZE_FLAG.write_text(f"{ts}\t{scrub(reason or 'freeze set')}\n", encoding="utf-8")
    except Exception as e:
        print(f"[screen_policy set_freeze failed] {repr(e)[:80]}", file=sys.stderr)


def clear_freeze() -> None:
    """Clear the global freeze (explicit human re-arm)."""
    try:
        _FREEZE_FLAG.unlink(missing_ok=True)
    except Exception as e:
        print(f"[screen_policy clear_freeze failed] {repr(e)[:80]}", file=sys.stderr)


# ===========================================================================
# Audit — every decision logged, secrets scrubbed, `value` NEVER logged raw
# ===========================================================================
def _audit(verb: str, target: str, risk_class: str | None, outcome: str, rung: str, reason: str) -> None:
    """Append one tab-separated decision line to logs/screen_actions.log. Best-effort; never raises.
    The `value` is represented only as [redacted] — its content never touches the log. target/reason/
    rung are run through the existing secret-scrub util (a token in a URL/element name can't leak)."""
    try:
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        row = "\t".join([
            ts,
            scrub(str(verb))[:60],
            scrub(str(target))[:200],
            str(risk_class),
            str(outcome),
            scrub(str(rung))[:40],
            "value=[redacted]",
            scrub(str(reason))[:200],
        ])
        with open(_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(row + "\n")
    except Exception as e:
        print(f"[screen_policy audit failed] {repr(e)[:80]}", file=sys.stderr)


def _finish(verb: str, target: str, risk_class: str | None,
            outcome: str, rung: str, reason: str) -> Decision:
    """Build the Decision, audit it, return it — the single exit point so EVERY path is logged once."""
    _audit(verb, target, risk_class, outcome, rung, reason)
    return Decision(outcome, risk_class, reason)


# ===========================================================================
# check_can_act(intent, deciding_rung) — the gate. Order: a..g; any failure -> DENY.
# ===========================================================================
def check_can_act(intent, deciding_rung) -> Decision:
    """Decide whether a proposed screen action may proceed. Returns a Decision whose outcome is
    'DENY' | 'CONFIRM_REQUIRED' | 'ALLOW'. In Phase 1 (watch-mode) ALLOW is NEVER returned — every
    permitted action is proposed for explicit human approval.

    Enforced order (any failure short-circuits to DENY):
      a. RE-VALIDATE  — recompute classify() from the concrete intent; ignore any caller class hint.
      b. FREEZE       — global freeze set -> DENY('frozen').
      c. BRAIN-FLOOR  — deciding rung not Groq/Claude -> DENY (rejects ollama/extractive/exhausted/unknown).
      d. CIRCUIT BREAKERS — max steps / rate / loop -> DENY.
      e. FORBIDDEN    -> DENY('forbidden').
      f. CONSEQUENTIAL -> CONFIRM_REQUIRED (always, never cached).
      g. TRIVIAL      -> CONFIRM_REQUIRED (watch-mode; nothing has earned auto yet).
    """
    # a. RE-VALIDATE from the concrete action itself (selfmod's defense-in-depth). Any class the
    #    caller stuffed into the intent is ignored — only verb+target decide.
    risk_class = classify(intent)
    verb = _verb(intent) or "?"
    target = _target(intent)
    rung = deciding_rung if isinstance(deciding_rung, str) else str(deciding_rung)

    # b. FREEZE — nothing acts while frozen.
    if is_frozen():
        return _finish(verb, target, risk_class, "DENY", rung, "frozen")

    # c. BRAIN-FLOOR — only the smart brain may drive an action.
    if not _is_smart_rung(deciding_rung):
        return _finish(verb, target, risk_class, "DENY", rung,
                       "brain-floor: smart brain required; pause and ask Nate")

    # d. CIRCUIT BREAKERS — record this attempt and trip if a limit is exceeded. Recording happens
    #    for every attempt that gets past freeze+brain-floor, so even denied-forbidden attempts count
    #    toward the rate/loop limits (an injection hammering the gate still gets rate-limited).
    trip = _breaker.record_and_check(verb, target)
    if trip:
        return _finish(verb, target, risk_class, "DENY", rung, trip)

    # e. FORBIDDEN -> DENY.
    if risk_class == "FORBIDDEN":
        return _finish(verb, target, risk_class, "DENY", rung, "forbidden")

    # f. CONSEQUENTIAL -> always an explicit confirmation (never auto, never cached).
    if risk_class == "CONSEQUENTIAL":
        return _finish(verb, target, risk_class, "CONFIRM_REQUIRED", rung,
                       "consequential action requires explicit confirmation")

    # g. TRIVIAL -> still CONFIRM_REQUIRED in watch-mode (no class has earned auto yet).
    if risk_class == "TRIVIAL":
        return _finish(verb, target, risk_class, "CONFIRM_REQUIRED", rung,
                       "trivial action proposed (watch-mode: confirm before acting)")

    # Fail-safe: an unexpected class -> DENY (default-deny).
    return _finish(verb, target, risk_class, "DENY", rung, "fail-safe default-deny")

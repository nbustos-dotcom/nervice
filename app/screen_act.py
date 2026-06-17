"""Screen Control PHASE 1 — the FIRST gated acting primitive (type_into), owns-nothing, watch-mode.

This is the first time Nervice can ACT on a page rather than only read it. It is built to be born
INSIDE the gate: the primitive cannot run except through a propose -> human-approve -> re-validate
flow, and every decision is made by app/screen_policy.check_can_act() (NOT reimplemented here).

The safety spine (mirrors the rest of the project's DNA):
  - SEMANTIC ONLY. Playwright role/label/placeholder locators — NEVER pixel/coordinate guessing
    (docs/SCREEN_CONTROL_ARCHITECTURE.md §1). type_into resolves a NAMED field, fills it, reads it
    back (verify-after-act), and stops. There is NO click/submit/navigate/press primitive in this
    module — so an injected "submit the form / click Buy" on a page CANNOT happen: the capability is
    absent (the browser.py "absence is enforcement" principle).
  - OWNS NOTHING (Tier U). The acting browser is a FRESH chromium.launch() with NO user_data_dir and
    new_context() with NO storage_state — it carries zero Canvas creds/cookies/profile. It is a
    SEPARATE browser from app/browser.py's persistent Canvas context (which is left read-only).
  - GATE-ONLY. type_into() refuses unless handed a one-shot, module-private approval token minted by
    approve_action() right after a fresh check_can_act() re-validation. No token -> refuse. A direct
    import-and-call of type_into() cannot act.
  - WATCH-MODE. Every action is PROPOSED and waits; nothing executes until an explicit human approve.
    Brain-floor (smart-brain-only) and FREEZE are enforced by the gate, re-checked at approve time.
  - KILL. An in-turn kill aborts any pending/in-flight action and tears down the owns-nothing context
    immediately (generalizes browser.close_browser).
  - AUDIT. Every propose/execute/result/abort is a {type:"hands"} event; the `value` is ALWAYS
    redacted to a length marker, never logged raw.

NOT wired into chat.py/router.py NL routing — the api.py /screen/* endpoints are a controlled harness.
This phase adds type_into ONLY (no click/submit/navigate). screen_policy's logic is untouched.
"""
import app.net  # noqa  (truststore: Norton TLS interception — needed for HTTPS navigation, mirror browser.py)

import asyncio
import datetime
import os
import pathlib
import re
import secrets
import sys
import time

from app import screen_policy          # THE GATE — the only thing that decides; never reimplemented here
from app.errorlog import scrub          # reuse the secret-scrub discipline for the hands log

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HANDS_LOG = _ROOT / "logs" / "screen_hands.log"

_PENDING_TTL = 300                      # a forgotten proposal expires after 5 min


# ===========================================================================
# {type:"hands"} TRACE — propose / execute / result / abort. value ALWAYS redacted.
# ===========================================================================
_HANDS_MAX = 200
_HANDS: list[dict] = []                 # in-memory ring of recent events (newest last)


def _redact_value(value) -> str:
    """A length marker — the ONLY representation of `value` that ever leaves this module's memory.
    The raw typed text is NEVER logged, traced, or surfaced (mirrors screen_policy's value=[redacted])."""
    n = len(value) if isinstance(value, str) else 0
    return f"[redacted:{n} chars]"


def _hands_emit(event: str, *, verb: str = "", field: str = "", url: str = "",
                value=None, outcome: str = "") -> dict:
    """Record one hands event to the ring + logs/screen_hands.log. Best-effort; never raises. Secrets
    scrubbed; value reduced to a length marker. `event` in propose|execute|result|abort|deny."""
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "type": "hands",
        "event": event,
        "verb": verb,
        "field": scrub(str(field))[:120],
        "url": scrub(str(url))[:200],
        "value": _redact_value(value),
        "outcome": scrub(str(outcome))[:200],
    }
    _HANDS.append(rec)
    if len(_HANDS) > _HANDS_MAX:
        del _HANDS[:-_HANDS_MAX]
    try:
        _HANDS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_HANDS_LOG, "a", encoding="utf-8") as f:
            f.write("\t".join([rec["ts"], event, verb, rec["field"], rec["url"],
                               rec["value"], rec["outcome"]]) + "\n")
    except Exception as e:
        print(f"[screen_act hands log failed] {repr(e)[:80]}", file=sys.stderr)
    return rec


def hands_events(n: int = 40) -> list[dict]:
    """The last n hands events, NEWEST FIRST (for GET /screen/hands)."""
    n = max(1, min(int(n), _HANDS_MAX))
    return list(reversed(_HANDS[-n:]))


# ===========================================================================
# OWNS-NOTHING acting browser — mirror browser.read_public_page, kept SEPARATE from browser.py.
# launch() with NO user_data_dir + new_context() with NO storage_state == carries zero creds.
# ===========================================================================
_act_pw = None
_act_browser = None
_act_ctx = None                         # the current in-flight acting context (so KILL can tear it down)
_act_lock = asyncio.Lock()


async def _ensure_act_browser():
    """Lazy owns-nothing chromium (singleton). NO user_data_dir, NO profile, NO cookies, NO creds —
    fully isolated from app/browser.py's persistent Canvas context. Visible by default so Nate
    watches (watch-mode); NERVICE_BROWSER_HEADLESS=1 forces headless (the test suite)."""
    global _act_pw, _act_browser
    if _act_browser is not None and _act_browser.is_connected():
        return _act_browser
    async with _act_lock:
        if _act_browser is not None and _act_browser.is_connected():
            return _act_browser
        from playwright.async_api import async_playwright
        _act_pw = await async_playwright().start()
        headless = os.environ.get("NERVICE_BROWSER_HEADLESS") == "1"
        _act_browser = await _act_pw.chromium.launch(
            headless=headless, args=["--no-first-run", "--no-default-browser-check"])
        print("[screen_act] launched OWNS-NOTHING acting browser (no profile/cookies/creds)", file=sys.stderr)
        return _act_browser


# URL policy for the acting primitive. Real Tier-U use is public https; file:///, localhost, and
# data:text/html are allowed for the CONTROLLED LOCAL TEST PAGE (the prompt's harness). javascript:/
# vbscript:/remote-http are refused. (The gate independently FORBIDs sensitive domains in the target.)
def _safe_url(url) -> str | None:
    u = (url or "").strip()
    low = u.lower()
    if low.startswith(("javascript:", "vbscript:", "blob:")):
        return None
    if re.match(r"^https://[^\s]+$", low):
        return u
    if re.match(r"^file:///[^\s]+$", low):
        return u
    if re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?(/[^\s]*)?$", low):
        return u
    if low.startswith("data:text/html"):
        return u
    return None


def _field_str(field) -> str:
    """A short, human/gate-readable string for a field locator (used in the gate target + traces)."""
    if isinstance(field, dict):
        return f"{field.get('by', 'label')}:{field.get('name') or field.get('role') or ''}".strip(":")
    return str(field or "")


async def _resolve_locator(page, field):
    """Resolve a SEMANTIC locator (role/label/placeholder/testid) — never a pixel/coordinate or raw
    CSS/XPath. A plain string is tried as label -> placeholder -> textbox role name. Returns a
    Playwright Locator with count>=1, or None if no semantic match (type_into then fails soft)."""
    try:
        if isinstance(field, dict):
            by = (field.get("by") or "label").lower()
            name = field.get("name") or ""
            if by == "label":
                loc = page.get_by_label(name)
            elif by == "placeholder":
                loc = page.get_by_placeholder(name)
            elif by == "role":
                loc = page.get_by_role(field.get("role") or "textbox", name=name)
            elif by == "testid":
                loc = page.get_by_test_id(name)
            else:
                return None
            return loc if await loc.count() > 0 else None
        name = str(field or "")
        for loc in (page.get_by_label(name), page.get_by_placeholder(name),
                    page.get_by_role("textbox", name=name)):
            if await loc.count() > 0:
                return loc
        return None
    except Exception:
        return None


# ===========================================================================
# ONE-SHOT APPROVAL TOKEN — type_into cannot run without it (gate-only execution).
# ===========================================================================
_APPROVAL_TOKEN: str | None = None      # minted by approve_action(), consumed by type_into() on use


async def type_into(url, field_locator, value, _approval=None) -> dict:
    """THE acting primitive — fills a NAMED field on a FRESH owns-nothing context and reads it back.

    GATE-ONLY: refuses unless `_approval` matches the current one-shot token minted by approve_action()
    after a fresh check_can_act() re-validation. A direct call (no/invalid token) is refused and
    traced — it cannot act. Semantic locator only; the ONLY DOM mutation is locator.fill() into the one
    named field (no click/submit/navigate exists). Fail-soft: returns {ok:False,...}, never raises."""
    global _APPROVAL_TOKEN, _act_ctx

    # --- HARD GUARD: gate-issued one-shot token required ---
    if not _APPROVAL_TOKEN or _approval != _APPROVAL_TOKEN:
        _hands_emit("abort", verb="type_into", field=_field_str(field_locator), url=str(url),
                    value=value, outcome="REFUSED — direct call without a gate-issued approval token")
        return {"ok": False, "refused": True,
                "error": "type_into is gate-only; it executes only via approve_action with a one-shot token."}
    _APPROVAL_TOKEN = None              # consume immediately (one-shot, never cached/reused)

    u = _safe_url(url)
    if u is None:
        _hands_emit("result", verb="type_into", field=_field_str(field_locator), url=str(url),
                    value=value, outcome="REFUSED — unsafe/unsupported URL scheme")
        return {"ok": False, "error": "Unsafe or unsupported URL (https / file:// / localhost / data: only)."}

    ctx = None
    try:
        br = await _ensure_act_browser()
        # new_context() with NO storage_state and NO user_data_dir = owns nothing (no Canvas creds).
        ctx = await br.new_context(user_agent="Mozilla/5.0 (compatible; NerviceActor/1.0; +owns-nothing)")
        _act_ctx = ctx
        page = await ctx.new_page()
        await page.goto(u, wait_until="domcontentloaded", timeout=15000)
        loc = await _resolve_locator(page, field_locator)
        if loc is None:
            _hands_emit("result", verb="type_into", field=_field_str(field_locator), url=u,
                        value=value, outcome="field not found (semantic locator matched nothing)")
            return {"ok": False, "error": f"Couldn't find a field matching {_field_str(field_locator)!r} on the page."}
        await loc.fill(value if isinstance(value, str) else str(value))   # SEMANTIC fill — the only mutation
        readback = await loc.input_value()                               # verify-after-act
        cookies = await ctx.cookies()                                    # owns-nothing proof: should be empty
        final_url = page.url
        verified = (readback == (value if isinstance(value, str) else str(value)))
        _hands_emit("result", verb="type_into", field=_field_str(field_locator), url=final_url,
                    value=value, outcome=f"typed; verify-after-act verified={verified}; "
                                         f"context_cookies={len(cookies)}")
        return {"ok": True, "verified": verified, "readback": readback,
                "field": _field_str(field_locator), "url": final_url,
                "owns_nothing_cookie_count": len(cookies)}
    except Exception as e:
        _hands_emit("result", verb="type_into", field=_field_str(field_locator), url=str(url),
                    value=value, outcome=f"FAIL {type(e).__name__}: {repr(e)[:80]}")
        return {"ok": False, "error": f"type_into failed ({type(e).__name__})."}
    finally:
        if ctx is not None:
            try:
                await ctx.close()                  # tear down the owns-nothing context every action
            except Exception:
                pass
        _act_ctx = None


# ===========================================================================
# GATED propose -> approve -> execute flow (mirror computer.resolve_pending / orchestrator _PENDING)
# ===========================================================================
_PENDING: dict | None = None            # the single staged action awaiting human yes/no (one-shot)


def _policy_intent(url, field) -> dict:
    """Build the STRING-target intent the gate classifies. The target carries BOTH the url and the
    field name so screen_policy's sensitive-domain + credential/oauth pattern checks can see them.
    `value` is deliberately NOT included — classify() must never be influenced by the typed text."""
    return {"verb": "type_into", "target_element": f"{url}  field={_field_str(field)}"}


async def propose_action(intent: dict, deciding_rung) -> dict:
    """Run a proposed type_into through screen_policy.check_can_act. DENY -> stop + report, nothing
    staged. CONFIRM_REQUIRED -> stage ONE pending action + emit a PROPOSED hands trace and WAIT —
    nothing executes. Mirrors the watch-mode gate; the gate alone decides."""
    global _PENDING
    if not isinstance(intent, dict) or intent.get("verb") != "type_into":
        return {"ok": False, "error": "Phase 1 supports only verb=type_into."}
    target = intent.get("target") or {}
    url, field, value = target.get("url"), target.get("field"), intent.get("value", "")
    if not url or not field:                         # missing field -> fail-safe refuse (never guess)
        return {"ok": False, "error": "Need both target.url and target.field for a type_into."}

    pol = _policy_intent(url, field)
    decision = screen_policy.check_can_act(pol, deciding_rung)

    if decision.outcome == "DENY":
        _PENDING = None                              # never leave anything staged on a deny
        _hands_emit("deny", verb="type_into", field=_field_str(field), url=str(url), value=value,
                    outcome=f"DENY ({decision.risk_class}): {decision.reason}")
        return {"ok": False, "outcome": "DENY", "risk_class": decision.risk_class, "reason": decision.reason}

    if decision.outcome == "CONFIRM_REQUIRED":
        _PENDING = {"url": url, "field": field, "value": value, "rung": deciding_rung,
                    "pol": pol, "ts": time.time()}
        _hands_emit("propose", verb="type_into", field=_field_str(field), url=str(url), value=value,
                    outcome="PROPOSED — awaiting human approval")
        return {"ok": True, "outcome": "CONFIRM_REQUIRED", "risk_class": decision.risk_class,
                "message": (f"Propose: type {_redact_value(value)} into "
                            f"{_field_str(field)!r} on {url} — approve? (approve / reject)")}

    # ALLOW is never returned in Phase 1; refuse defensively if the gate ever does.
    _PENDING = None
    _hands_emit("deny", verb="type_into", field=_field_str(field), url=str(url), value=value,
                outcome=f"unexpected gate outcome {decision.outcome} — refused")
    return {"ok": False, "outcome": decision.outcome, "reason": "unexpected gate outcome in Phase 1; refused."}


async def approve_action() -> dict:
    """Explicit human YES. RE-VALIDATE through check_can_act AGAIN (freeze/brain-floor/forbidden may
    have changed since propose — selfmod's defense-in-depth), then mint a one-shot token and execute
    type_into owns-nothing + verify-after-act. One-shot; the token is never cached."""
    global _PENDING, _APPROVAL_TOKEN
    p = _PENDING
    if not p:
        return {"ok": False, "error": "Nothing is pending to approve."}
    if time.time() - p.get("ts", 0) > _PENDING_TTL:
        _PENDING = None
        _hands_emit("abort", verb="type_into", field=_field_str(p["field"]), url=str(p["url"]),
                    value=p["value"], outcome="pending expired (TTL) — discarded")
        return {"ok": False, "error": "The pending action expired — propose it again."}

    # RE-VALIDATE from the concrete action right before it fires (the gate may now DENY).
    decision = screen_policy.check_can_act(p["pol"], p["rung"])
    if decision.outcome != "CONFIRM_REQUIRED":
        _PENDING = None
        _hands_emit("abort", verb="type_into", field=_field_str(p["field"]), url=str(p["url"]),
                    value=p["value"], outcome=f"re-validation blocked execution: {decision.outcome} ({decision.reason})")
        return {"ok": False, "outcome": decision.outcome, "reason": decision.reason,
                "note": "re-validation at approve time blocked execution (nothing typed)."}

    url, field, value = p["url"], p["field"], p["value"]
    _PENDING = None
    _APPROVAL_TOKEN = secrets.token_hex(16)           # one-shot token, scoped to THIS execution
    _hands_emit("execute", verb="type_into", field=_field_str(field), url=str(url), value=value,
                outcome="approved -> executing on owns-nothing context")
    try:
        res = await type_into(url, field, value, _approval=_APPROVAL_TOKEN)
    finally:
        _APPROVAL_TOKEN = None                        # ensure the token is dead even if type_into raised
    return {"ok": bool(res.get("ok")), "outcome": ("EXECUTED" if res.get("ok") else "FAILED"), "result": res}


async def reject_action() -> dict:
    """Explicit human NO — discard the pending action. Nothing is typed."""
    global _PENDING
    if not _PENDING:
        return {"ok": True, "note": "Nothing was pending."}
    p, _PENDING = _PENDING, None
    _hands_emit("abort", verb="type_into", field=_field_str(p["field"]), url=str(p["url"]),
                value=p["value"], outcome="REJECTED by human — discarded")
    return {"ok": True, "rejected": True}


async def kill() -> dict:
    """In-turn KILL / panic: abort any pending or in-flight action AND tear down the owns-nothing
    context immediately (generalizes browser.close_browser). Invalidates any outstanding approval
    token so an in-flight type_into cannot proceed, and closes the live context so a mid-flight
    Playwright call fails soft. Safe to call any time."""
    global _PENDING, _APPROVAL_TOKEN, _act_pw, _act_browser, _act_ctx
    had_pending = _PENDING is not None
    _PENDING = None
    _APPROVAL_TOKEN = None                            # kill any outstanding one-shot token
    ctx, br, pw = _act_ctx, _act_browser, _act_pw
    _act_ctx, _act_browser, _act_pw = None, None, None
    if ctx is not None:                                # close the in-flight context first
        try:
            await ctx.close()
        except Exception:
            pass
    if br is not None:
        try:
            await br.close()
        except Exception:
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
    _hands_emit("abort", verb="type_into", outcome=f"KILL — pending aborted={had_pending}, owns-nothing context torn down")
    return {"ok": True, "killed": True, "had_pending": had_pending}


def pending_summary() -> dict | None:
    """Read-only view of what's staged (for tests/observability). value redacted; never the raw text."""
    if not _PENDING:
        return None
    return {"verb": "type_into", "field": _field_str(_PENDING["field"]), "url": str(_PENDING["url"]),
            "value": _redact_value(_PENDING["value"]), "awaiting": True}

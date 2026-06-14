"""Spy-test for the in-code Claude spend guard (precautions #2 cap, #3 free-only, #4 confusion).

Run:  .venv/Scripts/python.exe scripts/test_claude_spend_guard.py

It NEVER makes a real Claude call. It replaces agent.query / agent.ClaudeSDKClient with tripwires
that raise if the SDK is ever reached, then drives each Claude entry point while the guard is
engaged and asserts: (a) ClaudeBlocked is raised, (b) the SDK tripwire fired ZERO times, (c) the
honest message is carried. Uses a TEMP usage file so the real data/usage.json is never touched.
"""
import sys
import json
import asyncio
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import usage
from app import agent

_results = []
def _ok(msg): _results.append(("PASS", msg)); print("  PASS", msg)
def _bad(msg): _results.append(("FAIL", msg)); print("  FAIL", msg)

# --- SDK tripwire: any real Claude call increments this and raises -------------------------------
_sdk_calls = {"n": 0}
def _spy_query(*a, **k):
    _sdk_calls["n"] += 1
    raise AssertionError("agent.query() was reached — the spend guard did NOT block the call!")
class _SpyClient:
    def __init__(self, *a, **k):
        _sdk_calls["n"] += 1
        raise AssertionError("ClaudeSDKClient was constructed — the spend guard did NOT block it!")
agent.query = _spy_query
agent.ClaudeSDKClient = _SpyClient

# --- temp ledger so we never pollute the real data/usage.json ------------------------------------
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="nervice-spendtest-"))
usage._DIR = _TMP
usage._FILE = _TMP / "usage.json"
usage._CONTROL_FILE = _TMP / "claude_control.json"   # isolate the free-only flag from real data/
_KEY = usage._today()
_STAGING = tempfile.mkdtemp(prefix="nervice-stage-")


async def _expect_blocked(label, factory, expect_msg):
    _sdk_calls["n"] = 0
    try:
        await factory()
        _bad(f"{label}: did NOT raise (expected ClaudeBlocked)")
    except agent.ClaudeBlocked as e:
        if _sdk_calls["n"] != 0:
            _bad(f"{label}: SDK reached {_sdk_calls['n']}x despite block")
        elif str(e) != expect_msg:
            _bad(f"{label}: wrong message {str(e)!r}")
        else:
            _ok(f"{label}: ClaudeBlocked raised, 0 SDK calls, honest message")
    except Exception as e:
        _bad(f"{label}: wrong exception {type(e).__name__}: {e}")


async def main():
    print("[#2] DAILY CAP — cap=0.01, today's spend seeded to 0.02 -> every Claude path must refuse")
    usage.CLAUDE_DAILY_CAP_USD = 0.01
    usage._data = {_KEY: {"claude_usd": 0.02}}
    if usage.claude_blocked_reason() == usage.BUDGET_MSG:
        _ok("claude_blocked_reason() returns the budget message")
    else:
        _bad(f"claude_blocked_reason() = {usage.claude_blocked_reason()!r}")
    await _expect_blocked("consult/ask_claude", lambda: agent.ask_claude("hello"), usage.BUDGET_MSG)
    await _expect_blocked("agent_task/build", lambda: agent.agent_task("build x"), usage.BUDGET_MSG)
    await _expect_blocked("browse_agent", lambda: agent.browse_agent("read x"), usage.BUDGET_MSG)
    await _expect_blocked("propose_agent/selfmod", lambda: agent.propose_agent("tweak", _STAGING), usage.BUDGET_MSG)

    print("[#2] UNDER CAP — cap=5.00, no spend -> guard allows (returns None)")
    usage.CLAUDE_DAILY_CAP_USD = 5.00
    usage._data = {}
    if usage.claude_blocked_reason() is None:
        _ok("under cap: claude_blocked_reason() is None (Claude allowed)")
    else:
        _bad(f"under cap: unexpectedly blocked: {usage.claude_blocked_reason()!r}")

    print("[#2] SPEND RECORDING — add_claude accumulates + persists to the daily ledger")
    usage._data = {}
    usage.add_claude(0.03)
    usage.add_claude(0.03)
    total = usage.today_claude_usd()
    if abs(total - 0.06) < 1e-9:
        _ok(f"today_claude_usd() == {total} after two $0.03 records")
    else:
        _bad(f"today_claude_usd() == {total}, expected 0.06")
    try:
        on_disk = json.loads(usage._FILE.read_text(encoding="utf-8"))
        if abs(on_disk.get(_KEY, {}).get("claude_usd", 0) - 0.06) < 1e-9:
            _ok("daily ledger persisted claude_usd to data/usage.json")
        else:
            _bad(f"ledger file missing/incorrect claude_usd: {on_disk}")
    except Exception as e:
        _bad(f"could not read ledger file: {e}")
    usage.add_claude(None); usage.add_claude(0)   # must be no-ops, never raise
    _ok("add_claude(None)/add_claude(0) are safe no-ops")

    print("[#3] FREE-ONLY — switch ON -> every Claude path short-circuits (cap NOT the blocker)")
    usage.CLAUDE_DAILY_CAP_USD = 5.00     # ensure the cap is not what's blocking
    usage._data = {}                      # zero spend today
    usage.set_free_only(True)
    if usage.free_only() is True:
        _ok("set_free_only(True) persisted -> free_only() is True")
    else:
        _bad("set_free_only(True) did not stick")
    if usage.claude_blocked_reason() == usage.FREE_ONLY_MSG:
        _ok("claude_blocked_reason() returns the free-only message (over the cap)")
    else:
        _bad(f"claude_blocked_reason() = {usage.claude_blocked_reason()!r}")
    await _expect_blocked("consult (free-only)", lambda: agent.ask_claude("hi"), usage.FREE_ONLY_MSG)
    await _expect_blocked("build (free-only)", lambda: agent.agent_task("x"), usage.FREE_ONLY_MSG)
    await _expect_blocked("browse (free-only)", lambda: agent.browse_agent("x"), usage.FREE_ONLY_MSG)
    await _expect_blocked("selfmod (free-only)", lambda: agent.propose_agent("x", _STAGING), usage.FREE_ONLY_MSG)
    usage.set_free_only(False)
    if usage.free_only() is False and usage.claude_blocked_reason() is None:
        _ok("free-only OFF + under cap -> Claude allowed again")
    else:
        _bad("free-only OFF did not restore Claude")

    print("[#4] CONFUSION — a repeat/correction spiral never yields an escalate-to-Claude signal")
    from app import confusion
    uid = "spendtest-user"
    confusion._HIST.pop(uid, None); confusion._PREV_BAD_END.pop(uid, None)
    spiral = ["play some music", "play some music", "no, that's not it",
              "I said play some music", "wrong, play music"]
    outs = [confusion.check(uid, t) for t in spiral]
    if all(o in (None, "hint") for o in outs):
        _ok(f"confusion.check never returned 'escalate' across the spiral (got {outs})")
    else:
        _bad(f"confusion.check produced an escalate signal: {outs}")
    if "hint" in outs:
        _ok("confusion still fires the FREE level-1 hint on repetition/correction")
    else:
        _bad("confusion produced no hint on an obvious spiral")
    confusion._HIST.pop(uid, None); confusion._PREV_BAD_END.pop(uid, None)
    confusion.note_turn_end(uid, "exhausted", False)   # an exhausted/guard-tripped prior turn
    if confusion.check(uid, "anything at all") == "hint":
        _ok("post-exhausted turn -> free hint, never an escalate")
    else:
        _bad("post-exhausted turn did not degrade to a free hint")


asyncio.run(main())
fails = [m for s, m in _results if s == "FAIL"]
print(f"\n=== {sum(1 for s,_ in _results if s=='PASS')} passed, {len(fails)} failed ===")
sys.exit(1 if fails else 0)

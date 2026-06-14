"""Result-loop v1 tests (paste-back -> propose -> approve). Run:
   .venv/Scripts/python.exe scripts/test_resultloop.py

Uses a TEMP state file + temp doc (never touches the real data/orchestrator_state.json / ACTIVE.md).
Parsing + next-prompt generation run on the FREE rung (Groq->Ollama); an SDK tripwire proves ZERO
Claude calls. Covers ok / fail / partial / garbage / cancel, plus keyword-net routing.
"""
import sys
import json
import asyncio
import pathlib
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from dotenv import load_dotenv
load_dotenv(str(_ROOT / ".env"))

from app import orchestrator as orch
from app import agent
from app import router

P, F = [], []
def ok(m): P.append(m); print("  PASS", m)
def bad(m): F.append(m); print("  FAIL", m)

# --- SDK tripwire: any Claude SDK call fails the test ---
_sdk = {"n": 0}
def _spy_query(*a, **k): _sdk["n"] += 1; raise AssertionError("agent.query reached — Claude was called!")
class _SpyClient:
    def __init__(self, *a, **k): _sdk["n"] += 1; raise AssertionError("ClaudeSDKClient constructed — Claude!")
agent.query = _spy_query
agent.ClaudeSDKClient = _SpyClient

# --- temp state + doc so the real project files are untouched ---
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="nervice-resultloop-"))
orch.STATE_FILE = _TMP / "orchestrator_state.json"
orch.ACTIVE_DOC = _TMP / "ACTIVE.md"
_DOC = ("# PROJECT — Pomodoro CLI\n\n## Goal\nA command-line pomodoro timer.\n\n"
        "## Requirements\n- 25 minute work timer\n- 5 minute break\n- a session counter\n\n"
        "## Success criteria\n- a full work+break cycle runs and the counter increments\n")
orch.ACTIVE_DOC.write_text(_DOC, encoding="utf-8")
_HASH = orch._doc_hash(_DOC)

def reset_state():
    orch._PENDING = {}
    steps = [
        {"title": "Create the 25-minute work timer", "detail": "timer.py counts down 25 minutes", "status": "pending"},
        {"title": "Add the 5-minute break",         "detail": "break logic after each work block", "status": "pending"},
        {"title": "Add a session counter",          "detail": "increment a counter each cycle",     "status": "pending"},
    ]
    orch.STATE_FILE.write_text(json.dumps(
        {"steps": steps, "current": 0, "plan_hash": _HASH, "doc_hash": _HASH, "prompts": {}}), encoding="utf-8")

def rung():  # the rung that last answered (must never be a claude account)
    return agent.current_rung.get()

def load():
    return json.loads(orch.STATE_FILE.read_text(encoding="utf-8"))


async def main():
    print("[router] keyword-net detects result paste -> orchestrator/result (no LLM)")
    for cue in ["here's what claude code said: created pomodoro.py, timer works",
                "result: ImportError on startup", "CC finished, tests passed"]:
        r = router._keyword_route(cue)
        (ok if r == {"route": "orchestrator", "op": "result"} else bad)(f"keyword-net routes {cue[:30]!r} -> {r}")

    print("[ok] paste a success report -> propose mark-done + next; yes advances + shows next prompt")
    reset_state()
    msg = await orch.propose_result("here's what claude code said: created timer.py, the 25-minute "
                                    "timer works, I ran it and it counts down correctly, tests pass")
    r1 = rung()
    if orch._PENDING.get("kind") == "result" and "done" in msg.lower():
        ok(f"ok-report -> proposal set (rung={r1}); msg: {msg[:70]!r}")
    else:
        bad(f"ok-report proposal wrong: pending={orch._PENDING.get('kind')} msg={msg[:80]!r}")
    reply = await orch.resolve_edit("yes")
    st = load(); r2 = rung()
    cond = st["steps"][0]["status"] == "done" and st["current"] == 1 and reply and "```" in (reply or "")
    (ok if cond else bad)(f"yes -> step0 done, current={st['current']}, next prompt shown (rung={r2})")
    (ok if not str(r1).startswith("claude") and not str(r2).startswith("claude") else bad)(
        f"ok path stayed on free rungs (parse={r1}, next-prompt={r2})")

    print("[fail] paste a failure report -> propose a fix step; yes inserts it after the failed step")
    reset_state()
    msg = await orch.propose_result("here's the result: I hit an ImportError and the timer crashes on "
                                    "startup, the tests failed")
    rf = rung()
    has_pending = orch._PENDING.get("kind") == "result"
    (ok if has_pending and "fail" in msg.lower() else bad)(f"fail-report -> fix proposal (rung={rf}); {msg[:70]!r}")
    reply = await orch.resolve_edit("yes")
    st = load()
    cond = len(st["steps"]) == 4 and st["steps"][1]["title"].lower().startswith("fix") and st["current"] == 0 \
        and st["steps"][0]["status"] == "pending"
    (ok if cond else bad)(f"yes -> fix step inserted at idx1, current stays 0, step0 still pending "
                          f"(steps={len(st['steps'])}, [1]={st['steps'][1]['title'][:30]!r})")

    print("[partial] paste a partial report -> propose follow-up; yes marks done + adds follow-up")
    reset_state()
    msg = await orch.propose_result("CC said: the 25 minute timer mostly works, but the session counter "
                                    "isn't implemented yet so it's incomplete")
    rp = rung()
    (ok if orch._PENDING.get("kind") == "result" else bad)(f"partial-report -> proposal (rung={rp}); {msg[:70]!r}")
    reply = await orch.resolve_edit("yes")
    st = load()
    cond = st["steps"][0]["status"] == "done" and len(st["steps"]) == 4 \
        and st["steps"][1]["title"].lower().startswith("follow") and st["current"] == 1
    (ok if cond else bad)(f"yes -> step0 done + follow-up at idx1, current={st['current']}")

    print("[garbage/empty] -> asks to paste the report, no crash, no pending")
    reset_state()
    m1 = await orch.propose_result("result:")            # cue only, empty report
    m2 = await orch.propose_result("asdf qwer zxcv lorem")  # not a report
    cond = ("paste" in m1.lower()) and (not orch._PENDING) and ("paste" in m2.lower() or "couldn't read" in m2.lower())
    (ok if cond else bad)(f"empty/garbage handled: m1={m1[:40]!r} m2={m2[:40]!r} pending={bool(orch._PENDING)}")

    print("[cancel] propose then 'no' -> nothing changes")
    reset_state()
    before = load()
    await orch.propose_result("here's what claude code said: created timer.py, it works, tests pass")
    reply = await orch.resolve_edit("no")
    after = load()
    (ok if after == before and not orch._PENDING else bad)(f"'no' -> state unchanged, pending cleared; reply={reply[:50]!r}")

    print(f"\n[SDK tripwire] Claude SDK calls during all scenarios: {_sdk['n']} (must be 0)")
    (ok if _sdk["n"] == 0 else bad)("ZERO Claude SDK calls across the whole result loop")


asyncio.run(main())
print(f"\n=== {len(P)} passed, {len(F)} failed ===")
sys.exit(1 if F else 0)

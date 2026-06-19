"""The watcher loop - run a MULTI-STEP Claude Code job HANDS-OFF in a sandbox, bounded and serial.

Plan a step (FREE Groq) -> run it (run_headless_step: sandbox-guarded, budget-gated, git-verified) ->
check it -> decide done-or-next -> repeat. At the end it HALTS for human review: it never pushes,
merges, or leaves the sandbox. Runs as a background task so it can work while Nate is away.

Every safety property is INHERITED from the proven primitive, not re-implemented:
  - SANDBOX-ONLY + per-step git cross-check + per-step budget gate + per-step timeout all live inside
    run_headless_step (app/cc_headless.py). This module only orchestrates calls to it.
  - PLANNING IS FREE: the next-step prompt and the done-check run on orchestrator._ask_free (Groq ->
    local Ollama -> honest none). NEVER Claude. The only metered work is the coding step itself.

Hard rules enforced here:
  - BOUNDED: max_steps (default _MAX_STEPS_DEFAULT, hard-capped at _MAX_STEPS_CEILING).
  - STOP ON ANY FAILURE: a step that lands no real work (not committed / empty diff / error / timeout)
    STOPS the loop and reports which step + why. It NEVER chains past a bad step (silent-failure defense).
  - PAUSE on a budget-block / fail-closed ledger / rate-limit -> stop with a clear paused reason; no retry.
  - SERIAL: only ONE loop at a time (matches the headless ledger's no-lock constraint). A second start
    is refused while one is active.
  - HALT FOR REVIEW: on done / stopped / paused / max-steps it writes a structured report + audit log
    (data/headless_runs/) and stops. No push, no merge, nothing outside the sandbox.

Does NOT touch usage.py's $ guard, safety.py, selfmod.py, SAFETY_FLOOR, or agent.py's key scrub.
Never sets ANTHROPIC_API_KEY. No new spawn path - it only calls the existing run_headless_step.
"""
import os
import re
import sys
import json
import asyncio
import pathlib
import subprocess
import importlib.util
from datetime import datetime
from zoneinfo import ZoneInfo

from app import cc_headless, orchestrator   # reuse: the one-step runner+guard, and the FREE planner/git helpers
from app import agent                        # SECOND BRAIN: reuse the guarded read-only Claude reasoning call

_TZ = ZoneInfo("America/Chicago")
_RUNDIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "headless_runs"   # audit trail (gitignored)

# ---- BOUNDS (clearly-labeled config constants) ----
_MAX_STEPS_DEFAULT = 8      # default ceiling on steps/goal - meaty complete-component steps need a bit more room
_MAX_STEPS_CEILING = 12     # hard upper bound even if a caller asks for more
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")   # filesystem-safe run ids only

# ---- SERIAL state: one loop at a time. In-memory run registry + the active run id. ----
_runs: dict = {}
_tasks: dict = {}
_active_run_id = None

_PLAN_SYS = (
    "You drive a HANDS-OFF coding loop in a throwaway sandbox git repo. Given a GOAL and the PROGRESS so "
    "far (committed files + git log), output the instruction for the NEXT COMPLETE COMPONENT - one whole, "
    "self-contained, committable deliverable that is not yet built. A COMPONENT is an ENTIRE module with "
    "ALL of its functions/methods finished AND its tests, together in the SAME step (for example: the full "
    "data model with every method plus its test file; or the storage layer plus its tests; or the CLI with "
    "EVERY command wired up). NEVER a skeleton or stub, NEVER one method/function at a time, NEVER half a "
    "module left for a later step. Look at PROGRESS and build only what is still MISSING: do NOT re-create, "
    "rename, or re-do a component that already exists in the tracked files, and do NOT invent components the "
    "GOAL did not ask for. Finish the GOAL in as FEW complete steps as possible. Build the CODE components "
    "first; then, as soon as the code files the GOAL names already exist in the tracked files, your NEXT "
    "component MUST be the README/docs the GOAL asks for - do NOT write or extend any more code or test "
    "files, just create the README and commit it. "
    "Write a SHORT, directive instruction that NAMES the exact file(s) and the functions/methods/commands "
    "and tests to build - do NOT paste full code implementations (that buries the commit step). Tests MUST "
    "use Python's built-in `unittest` and be runnable with `python -m unittest` - NEVER pytest or any other "
    "third-party test framework - and the agent must actually run them and see them pass before committing. "
    "COMMITTING IS MANDATORY and is the agent's FINAL action. The instruction MUST end with a clearly "
    "separated final step, exactly: `FINALLY, run: git add -A && git commit -m \"<short message>\"` - and "
    "state that the step is NOT complete and WILL BE REJECTED unless it makes the tests pass AND commits. "
    "Do NOT ask questions - the agent runs unattended. Output ONLY the instruction text to hand the coding "
    "agent - no preamble, no JSON, no options.")

_DONE_SYS = (
    "You judge whether a coding GOAL is fully satisfied by the work COMMITTED so far in a sandbox. Given the "
    "GOAL and the PROGRESS (committed files + git log), answer ONLY JSON: "
    "{\"done\": true|false, \"reason\": \"<one short sentence>\"}. Say done=true ONLY when EVERY part of the "
    "goal exists and is committed; if any required file or behavior is missing, done=false.")


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _git_log(ws: pathlib.Path) -> list:
    out = orchestrator._git(ws, "log", "--oneline", "-n", "40")
    return [ln for ln in (out or "").splitlines() if ln.strip()]


def _progress(ws: pathlib.Path) -> str:
    """What's been COMMITTED so far - the planner's and done-check's view of the world."""
    log = orchestrator._git(ws, "log", "--oneline", "-n", "40") or "(no commits yet)"
    files = orchestrator._git(ws, "ls-files") or "(none)"
    return f"COMMITS (newest first):\n{log}\n\nTRACKED FILES:\n{files}"


# ---- LOCAL TEST GATE: after a step commits, RUN the sandbox project's tests (no Claude/Groq cost).
# A step is only "ok" if its tests PASS; tests that fail/error STOP the loop (same as any step failure).
# No recognizable tests -> ran=False, and the loop falls back to run_headless_step's commit-only check
# (don't block - e.g. a README step). Tests run with THIS interpreter (sys.executable) - fine for the
# stdlib-only projects the planner targets; a project needing its own deps is an UNKNOWN.
_TEST_TIMEOUT_S = 120   # bounded; kill on overrun (PID-tree)


def _find_test_files(ws: pathlib.Path) -> list:
    """Recognizable Python test files: test_*.py / *_test.py at the root or under tests/ or test/."""
    out = []
    for base in (ws, ws / "tests", ws / "test"):
        if base.is_dir():
            out += list(base.glob("test_*.py")) + list(base.glob("*_test.py"))
    return sorted(set(out))


def _looks_pytest(ws: pathlib.Path, test_files: list) -> bool:
    """True if the project is pytest-shaped: a pytest config, OR a test file that imports pytest."""
    for cfg in ("pytest.ini", "conftest.py", "tox.ini"):
        if (ws / cfg).exists():
            return True
    for cfg, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]")):
        p = ws / cfg
        if p.exists():
            try:
                if marker in p.read_text(encoding="utf-8", errors="replace"):
                    return True
            except Exception:
                pass
    for f in test_files:
        try:
            if "import pytest" in f.read_text(encoding="utf-8", errors="replace"):
                return True
        except Exception:
            pass
    return False


def _test_summary(text: str, passed: bool) -> str:
    """One concise line from unittest/pytest output for the step record."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    ran = next((ln for ln in reversed(lines) if ln.startswith("Ran ")), "")            # unittest "Ran N tests in Xs"
    verdict = next((ln for ln in reversed(lines)
                    if ln == "OK" or ln.startswith("OK ") or ln.startswith("FAILED")), "")  # unittest verdict
    if ran or verdict:
        return (ran + (" - " if ran and verdict else "") + verdict).strip()[:200]
    ps = next((ln for ln in reversed(lines)
               if any(w in ln for w in ("passed", "failed", "error", "no tests ran"))), "")  # pytest summary
    if ps:
        return ps.strip("= ").strip()[:200]
    return (lines[-1][:200] if lines else ("tests passed" if passed else "tests failed"))


async def run_tests(workspace) -> dict:
    """Run the sandbox project's tests LOCALLY (no Claude/Groq). Python: `python -m unittest discover`
    (stdlib), or pytest ONLY if the project is pytest-shaped AND pytest is installed. Returns
    {ran, passed, summary, output_tail}; no recognizable tests -> ran=False. Timeout-bounded with a
    PID-tree kill on overrun. Never raises. NOTE: pytest-style tests with pytest NOT installed fall to
    `unittest discover`, which errors on `import pytest` - so a test that can't even run is CAUGHT."""
    ws = pathlib.Path(workspace)
    test_files = _find_test_files(ws)
    if not test_files:
        return {"ran": False, "passed": None, "summary": "no tests to verify", "output_tail": ""}
    py = sys.executable
    use_pytest = (importlib.util.find_spec("pytest") is not None) and _looks_pytest(ws, test_files)
    args = ([py, "-m", "pytest", "-q"] if use_pytest
            else [py, "-m", "unittest", "discover", "-p", "test*.py"])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(ws), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except Exception as e:
        return {"ran": True, "passed": False, "summary": f"could not launch tests: {type(e).__name__}",
                "output_tail": ""}
    pid = proc.pid
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TEST_TIMEOUT_S)
        text = (out or b"").decode("utf-8", "replace")
        passed = (proc.returncode == 0)
        return {"ran": True, "passed": passed, "summary": _test_summary(text, passed),
                "runner": "pytest" if use_pytest else "unittest", "output_tail": text[-1600:]}
    except asyncio.TimeoutError:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return {"ran": True, "passed": False, "summary": f"tests exceeded {_TEST_TIMEOUT_S}s - killed",
                "output_tail": ""}


# ---- SECOND BRAIN: Claude reasons over Groq's draft step (READ-ONLY, ONE turn) before CC runs it.
# Reuses agent.ask_claude -> agent._ask_one (max_turns=1, allowed_tools=[] - no tools, no acting), gated
# by agent._spend_guard (usage.claude_blocked_reason: $5/day cap + free_only) BEFORE any SDK call, with the
# key-scrub + subscription-only auth. Best-effort: ANY block / exhaustion / error / timeout falls back to
# Groq's draft unchanged, records second_brain="skipped:<reason>", and NEVER hard-blocks the loop.
try:
    _CRITIQUE_TIMEOUT_S = float(os.environ.get("NERVICE_CRITIQUE_TIMEOUT_S") or 60.0)
except (TypeError, ValueError):
    _CRITIQUE_TIMEOUT_S = 60.0

_CRITIQUE_SYS = (
    "You are the LOGIC CHECK in a two-model planning loop for a hands-off coding agent (CC) working in a "
    "throwaway sandbox git repo. A first model (Groq) drafted the next step prompt. Reason over the GOAL, the "
    "PROGRESS so far (git log + tracked files), and Groq's DRAFT, then return a CORRECTED, concrete step "
    "prompt for CC. Catch and fix: scope-creep (more than ONE component in a step), redoing work already "
    "committed in PROGRESS, wrong tooling (a non-standard-library dependency when the GOAL says standard "
    "library only; pytest instead of the stdlib unittest), and hallucinated requirements the GOAL never "
    "asked for. Keep whatever is already correct. The result MUST stay ONE complete, self-contained "
    "component (a whole module with all its functions/methods AND its stdlib-unittest tests, or the README "
    "once the code exists) and MUST end with the final step `FINALLY, run: git add -A && git commit -m "
    "\"<short message>\"`. If the draft is already correct, return it essentially unchanged. Output ONLY the "
    "final step-prompt text to hand CC - no preamble, no analysis, no options.")


async def _claude_critique(goal: str, progress: str, draft: str) -> tuple:
    """SECOND BRAIN. Claude reasons over Groq's draft and returns (refined_prompt, "claude"). READ-ONLY:
    agent.ask_claude is one turn, no tools, subscription-auth, $5-cap/free-only guarded. Best-effort -
    ANY block / exhaustion / error / timeout -> (None, "skipped:<reason>"). NEVER raises (the loop must
    never hard-block on the second brain)."""
    task = (f"GOAL:\n{goal}\n\nPROGRESS SO FAR (git log + tracked files):\n{progress}\n\n"
            f"GROQ'S DRAFT STEP PROMPT:\n{draft}\n\nReturn the single corrected step prompt for CC now.")
    # REAL timeout: the Agent SDK swallows asyncio cancellation (wait_for can't interrupt it), so we stop
    # WAITING after the timeout and ABANDON a slow turn rather than letting it stall the loop. ask_claude
    # itself is already bounded (max_turns=1, no tools), so a normal turn finishes well within the timeout.
    fut = asyncio.ensure_future(agent.ask_claude(task, system=_CRITIQUE_SYS))
    done, _ = await asyncio.wait({fut}, timeout=_CRITIQUE_TIMEOUT_S)
    if fut not in done:                                # slow/hung turn -> abandon it, fall back to Groq
        fut.cancel()
        fut.add_done_callback(lambda t: t.cancelled() or t.exception())   # swallow its later result/exc
        return None, "skipped:timeout"
    try:
        out = (fut.result() or "").strip()
        return (out, "claude") if out else (None, "skipped:claude-empty")
    except agent.CLIUnavailable:
        return None, "skipped:cli-down"                # SDK CLI couldn't launch (zero spend)
    except agent.ClaudeBlocked:
        return None, "skipped:blocked"                 # $5 cap reached or free-only ON (zero spend)
    except agent.AllClaudeExhausted:
        return None, "skipped:exhausted"               # every Claude account failed
    except Exception as e:
        return None, f"skipped:{type(e).__name__}"


async def _plan_step(goal: str, progress: str, n: int, ms: int) -> tuple:
    """TWO-BRAIN plan. Groq (Nervice's intent) drafts the next step; then Claude (the logic check)
    reasons over goal + progress + draft and returns a refined prompt for CC. Returns (prompt, rung,
    brains) where brains = {groq_draft, refined_prompt, second_brain} for the step record. rung 'none' =
    free Groq exhausted. Best-effort second brain: any Claude failure -> Groq's draft unchanged."""
    user = (f"GOAL:\n{goal}\n\nPROGRESS SO FAR:\n{progress}\n\nThis is step {n} of at most {ms}. Write the "
            f"instruction for the NEXT COMPLETE COMPONENT not yet built - a whole module with ALL its "
            f"functions/methods AND its tests in this one step - then commit it. Use as few steps as "
            f"possible; leave README/docs for last.")
    draft, rung = await orchestrator._ask_free(_PLAN_SYS, user, want_json=False, num_predict=400)
    draft = (draft or "").strip()
    if rung == "none" or not draft:                    # no Groq draft -> nothing to critique
        return draft, rung, {"groq_draft": draft, "refined_prompt": draft, "second_brain": "skipped:no-groq-draft"}
    refined, second_brain = await _claude_critique(goal, progress, draft)
    final = refined if second_brain == "claude" else draft           # transparent fallback to Groq's draft
    brains = {"groq_draft": draft[:700], "refined_prompt": final[:700], "second_brain": second_brain}
    return final, rung, brains


async def _check_done(goal: str, progress: str) -> tuple:
    """FREE completion judgment. Returns (done: bool, reason: str). Unavailable free rung -> (False, ...)
    so the loop keeps going under max_steps rather than falsely declaring done."""
    user = f"GOAL:\n{goal}\n\nPROGRESS:\n{progress}\n\nIs every part of the goal built and committed?"
    obj, rung = await orchestrator._ask_free(_DONE_SYS, user, want_json=True, num_predict=200)
    if rung == "none" or not isinstance(obj, dict):
        return False, "done-check unavailable (free rung) - continuing under max_steps"
    return bool(obj.get("done")), str(obj.get("reason") or "")[:200]


def _classify(run: dict, n: int, prompt: str, r: dict) -> dict:
    """Record the step, then decide whether the loop STOPS. Order matters: paused conditions (budget /
    fail-closed / rate-limit) and timeout are distinguished from a plain no-work FAILURE. A successful,
    committed, non-empty-diff step is the ONLY 'continue'."""
    git = r.get("git") or {}
    spawned = bool((r.get("run") or {}).get("spawned"))
    rec = {"n": n, "prompt": (prompt or "")[:300], "ok": bool(r.get("ok")),
           "committed": r.get("committed"), "commit": (git.get("subject") or ""),
           "summary": r.get("summary") or r.get("error") or "",
           "reported_cost": (r.get("result_meta") or {}).get("reported_cost") or 0.0,
           "rate_limited": bool(r.get("rate_limited")), "spawned": spawned}
    run["steps"].append(rec)
    _persist(run)
    if r.get("blocked"):                       # usage.py $ cap / free-only backstop
        return {"stop": True, "status": "paused", "reason": f"step {n}: spend guard ($ cap / free-only) - {r.get('error')}"}
    if r.get("blocked_headless_budget"):        # headless run-count/cost gate, incl. fail-closed ledger
        return {"stop": True, "status": "paused", "reason": f"step {n}: headless budget gate - {r.get('reason')}"}
    if r.get("rate_limited"):                   # subscription rate/usage limit - pause, do NOT retry
        return {"stop": True, "status": "paused", "reason": f"step {n}: subscription rate/usage limit - not retried"}
    if (r.get("run") or {}).get("timed_out"):
        return {"stop": True, "status": "failed", "reason": f"step {n}: timed out - {r.get('error')}"}
    if not r.get("ok"):                         # not committed / empty diff / spawn error -> STOP, no chaining
        return {"stop": True, "status": "failed",
                "reason": f"step {n} landed no real work (NOT chaining past it): {rec['summary']}"}
    return {"stop": False}


def _finish(run: dict, status: str, reason: str) -> None:
    run["status"] = status
    run["reason"] = reason
    run["current_step"] = "halted for review"
    run["ended_ts"] = _now()
    run["git_log"] = _git_log(pathlib.Path(run["workspace"]))
    _persist(run)
    print(f"[headless_loop] {run['run_id']} -> {status}: {reason}", file=sys.stderr)


async def _run_loop(run_id: str) -> None:
    """The serial loop body. Updates the in-memory run dict (read by GET) and persists an audit log.
    Always clears the active-run flag on exit (finally) so a new loop can start."""
    global _active_run_id
    run = _runs[run_id]
    ws = pathlib.Path(run["workspace"]); goal = run["goal"]; ms = run["max_steps"]
    try:
        for n in range(1, ms + 1):
            run["current_step"] = f"planning step {n}/{ms}"; _persist(run)
            prompt, rung, brains = await _plan_step(goal, _progress(ws), n, ms)
            if rung == "none" or not prompt:
                _finish(run, "paused", f"planning unavailable (free rung exhausted) before step {n}")
                return
            run["current_step"] = f"running step {n}/{ms}"; _persist(run)
            r = await cc_headless.run_headless_step(str(ws), prompt)     # the ONE proven gated+verified step
            verdict = _classify(run, n, prompt, r)
            if run["steps"]: run["steps"][-1].update(brains)            # observability: both brains in the step record
            if verdict["stop"]:
                _finish(run, verdict["status"], verdict["reason"])
                return
            # TEST GATE (additive): the step committed (git check ok) - now require its tests to PASS.
            # Runs LOCALLY in the sandbox (no Claude/Groq). No recognizable tests -> commit-only fallback.
            run["current_step"] = f"testing step {n}/{ms}"; _persist(run)
            tr = await run_tests(ws)
            if run["steps"]:
                run["steps"][-1]["tests"] = {k: tr.get(k) for k in ("ran", "passed", "summary")}
            if tr["ran"] and not tr["passed"]:               # tests ran and FAILED/errored -> STOP, no chaining
                if run["steps"]:
                    run["steps"][-1]["ok"] = False
                _finish(run, "failed", f"step {n}: committed but TESTS FAILED - {tr['summary']}")
                return
            run["steps_done"] = n; _persist(run)
            run["current_step"] = f"checking goal after step {n}/{ms}"
            done, why = await _check_done(goal, _progress(ws))
            if done:
                _finish(run, "done", f"goal complete after {n} step(s): {why}")
                return
        _finish(run, "max_steps", f"reached max_steps={ms} without the goal being judged complete")
    except Exception as e:
        _finish(run, "error", f"loop crashed: {type(e).__name__}: {repr(e)[:160]}")
    finally:
        if _active_run_id == run_id:
            _active_run_id = None


def start_goal_run(workspace, goal, max_steps=None) -> dict:
    """Validate (sandbox guard + git repo + goal), enforce the SERIAL rule, then launch the loop as a
    background task and return immediately. Refuses (no task) if a loop is already active or the inputs
    are bad. Must be called from within the server's running event loop (the async endpoint)."""
    global _active_run_id
    if _active_run_id is not None and _runs.get(_active_run_id, {}).get("status") == "running":
        return {"started": False, "refused": True, "active_run_id": _active_run_id,
                "reason": f"a headless loop is already running ({_active_run_id}) - only one at a time (serial)"}
    allowed, info = cc_headless._check_workspace(workspace)              # REUSE the sandbox-only guard
    if not allowed:
        return {"started": False, "refused": True, "reason": info}
    ws = pathlib.Path(info)
    if not orchestrator._git_is_repo(ws):
        return {"started": False, "refused": True, "reason": f"'{ws}' is not a git repo - the loop needs a repo to commit into."}
    if not goal or not str(goal).strip():
        return {"started": False, "refused": True, "reason": "no goal given."}
    try:
        ms = _MAX_STEPS_DEFAULT if not max_steps else max(1, min(int(max_steps), _MAX_STEPS_CEILING))
    except (TypeError, ValueError):
        ms = _MAX_STEPS_DEFAULT
    run_id = datetime.now(_TZ).strftime("%Y%m%d-%H%M%S") + "-" + ("%04x" % (abs(hash(str(ws) + goal)) & 0xFFFF))
    _runs[run_id] = {"run_id": run_id, "workspace": str(ws), "goal": str(goal).strip(), "max_steps": ms,
                     "status": "running", "steps_done": 0, "current_step": "starting", "steps": [],
                     "reason": None, "started_ts": _now(), "ended_ts": None}
    _active_run_id = run_id
    _persist(_runs[run_id])
    _tasks[run_id] = asyncio.create_task(_run_loop(run_id))             # background on the server loop
    return {"started": True, "run_id": run_id, "status": "started", "max_steps": ms}


def _build_report(run: dict) -> dict:
    steps = run.get("steps", [])
    git_log = run.get("git_log")
    if git_log is None:                                                  # in-flight: read live (sandbox still present)
        try:
            git_log = _git_log(pathlib.Path(run["workspace"]))
        except Exception:
            git_log = []
    return {
        "run_id": run["run_id"], "status": run["status"], "reason": run.get("reason"),
        "goal": run["goal"], "workspace": run["workspace"], "max_steps": run["max_steps"],
        "steps_done": run.get("steps_done", 0),
        "total_runs": sum(1 for s in steps if s.get("spawned")),
        "total_reported_cost": round(sum((s.get("reported_cost") or 0.0) for s in steps), 6),
        "steps": steps, "git_log": git_log,
        "started_ts": run.get("started_ts"), "ended_ts": run.get("ended_ts"),
        "note": "HALTED for human review - the loop never pushes, merges, or leaves the sandbox.",
    }


def get_status(run_id: str):
    """Read-only status for GET /loop/run-status/{run_id}. Loads from the audit log if not in memory
    (survives a restart). Returns None for an unknown/invalid id."""
    if not run_id or not _RUN_ID_RE.match(run_id):
        return None
    run = _runs.get(run_id) or _load_run(run_id)
    if not run:
        return None
    return {"status": run["status"], "steps_done": run.get("steps_done", 0),
            "current_step": run.get("current_step"), "report": _build_report(run)}


def _persist(run: dict) -> None:
    try:
        _RUNDIR.mkdir(parents=True, exist_ok=True)
        (_RUNDIR / f"{run['run_id']}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[headless_loop] persist failed: {repr(e)[:80]}", file=sys.stderr)


def _load_run(run_id: str):
    try:
        p = _RUNDIR / f"{run_id}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

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
import re
import sys
import json
import asyncio
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from app import cc_headless, orchestrator   # reuse: the one-step runner+guard, and the FREE planner/git helpers

_TZ = ZoneInfo("America/Chicago")
_RUNDIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "headless_runs"   # audit trail (gitignored)

# ---- BOUNDS (clearly-labeled config constants) ----
_MAX_STEPS_DEFAULT = 5      # default ceiling on steps per goal - deliberately LOW
_MAX_STEPS_CEILING = 12     # hard upper bound even if a caller asks for more
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")   # filesystem-safe run ids only

# ---- SERIAL state: one loop at a time. In-memory run registry + the active run id. ----
_runs: dict = {}
_tasks: dict = {}
_active_run_id = None

_PLAN_SYS = (
    "You drive a HANDS-OFF coding loop in a throwaway sandbox git repo. Given a GOAL and the PROGRESS so "
    "far (committed files + git log), output the instruction for the NEXT SINGLE PIECE only - the smallest "
    "unit of the goal that is not yet done. Rules: build ONE file or ONE function, NOT the whole goal; if a "
    "test is required, make it pass; then run `git add -A && git commit -m \"<short message>\"`. Keep it "
    "minimal and self-contained. Do NOT bundle multiple pieces. Do NOT ask questions - the agent runs "
    "unattended. Output ONLY the instruction text to hand the coding agent - no preamble, no JSON, no options.")

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


async def _plan_step(goal: str, progress: str, n: int, ms: int) -> tuple:
    """FREE next-step prompt from goal + progress. Returns (prompt, rung). rung 'none' = free exhausted."""
    user = (f"GOAL:\n{goal}\n\nPROGRESS SO FAR:\n{progress}\n\nThis is step {n} of at most {ms}. Write the "
            f"NEXT single-piece instruction (build ONE small unit not yet done, then commit it).")
    prompt, rung = await orchestrator._ask_free(_PLAN_SYS, user, want_json=False, num_predict=400)
    return (prompt or "").strip(), rung


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
            prompt, rung = await _plan_step(goal, _progress(ws), n, ms)
            if rung == "none" or not prompt:
                _finish(run, "paused", f"planning unavailable (free rung exhausted) before step {n}")
                return
            run["current_step"] = f"running step {n}/{ms}"; _persist(run)
            r = await cc_headless.run_headless_step(str(ws), prompt)     # the ONE proven gated+verified step
            verdict = _classify(run, n, prompt, r)
            if verdict["stop"]:
                _finish(run, verdict["status"], verdict["reason"])
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

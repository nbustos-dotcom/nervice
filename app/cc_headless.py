"""Headless Claude Code — run ONE coding step hands-off in a DISPOSABLE sandbox, git-verified.

First slice of the autonomous flagship: Nervice writes the step prompt to <ws>/.nervice/prompt.md
(the orchestrator's existing handoff), invokes `claude -p` non-interactively in the sandbox, then
VERIFIES the result against git — a real commit AND a non-empty diff (the orchestrator's empty-diff
honesty check). NO human paste. ONE step only — multi-step auto-advance (the watcher) is a LATER build.

Safety is in CODE, not hope:
  - DISPOSABLE-WORKSPACE GUARD (default-deny): the cwd must be under a temp/sandbox root AND must not
    be the Nervice repo or any live project (StudyNerve / ai-teacher). Everything else is REFUSED.
  - SUBSCRIPTION AUTH ONLY: CLAUDE_CONFIG_DIR (agent.CONFIG_DIR) + agent._SCRUB_KEYS popped from the
    child env. NEVER sets ANTHROPIC_API_KEY. Honours the $5 cap / free_only via usage.claude_blocked_reason.
  - LEAST-DANGEROUS MODE: acceptEdits (NOT skip-all) + agent's _BUILDER_TOOLS allowlist — can still
    edit+commit in the sandbox without prompting.
  - BOUNDED: hard 5-min timeout -> PID-tree kill on overrun.
  - VERIFY, DON'T TRUST: a "done" with no commit / empty diff is FLAGGED ok=False.

Does NOT touch safety.py / selfmod.py / SAFETY_FLOOR / the spend guard. NEVER sets an API key.
"""
import asyncio
import os
import platform
import subprocess
import tempfile
from pathlib import Path

from app import agent, usage, orchestrator   # reuse: CONFIG_DIR/scrub/tools, the $ cap, git+prompt handoff
from app import headless_budget               # SEPARATE, additive run-count/cost gate + usage ledger (fail-closed)
from app.errorlog import scrub

_NERVICE_ROOT = Path(__file__).resolve().parent.parent
# The ONLY parent dirs a headless run may live under — DEFAULT-DENY everything else.
_SANDBOX_ROOTS = [Path(tempfile.gettempdir()).resolve(), (Path.home() / "nervice-cc-sandbox").resolve()]
# Live projects HARD-blocked even if a path tries to look sandbox-y (defense in depth).
_BLOCKED_SUBSTRINGS = ["studynerve", "ai-teacher", "ai_teacher"]
_HEADLESS_TIMEOUT_S = 300   # 5-min hard bound


def _check_workspace(workspace) -> tuple[bool, str]:
    """DEFAULT-DENY: allow ONLY a throwaway sandbox/temp dir; hard-block the Nervice repo + live
    projects. Returns (allowed, resolved_path) or (False, refusal_message)."""
    if not workspace or not str(workspace).strip():
        return False, "REFUSED: no workspace given."
    try:
        p = Path(str(workspace)).expanduser().resolve()
    except Exception as e:
        return False, f"REFUSED: unresolvable path ({type(e).__name__})."
    s = str(p).replace("\\", "/").lower()
    nr = str(_NERVICE_ROOT).replace("\\", "/").lower()
    if s == nr or s.startswith(nr + "/"):
        return False, f"REFUSED: '{p}' is inside the Nervice repo — a headless coding step must NEVER run here."
    for b in _BLOCKED_SUBSTRINGS:
        if b in s:
            return False, f"REFUSED: '{p}' looks like a live project (matched '{b}') — headless runs are sandbox-only."
    under = any(s == str(r).replace("\\", "/").lower() or s.startswith(str(r).replace("\\", "/").lower() + "/")
                for r in _SANDBOX_ROOTS)
    if not under:
        roots = " or ".join(str(r) for r in _SANDBOX_ROOTS)
        return False, (f"REFUSED (default-deny): '{p}' is not under an allowed sandbox/temp root ({roots}). "
                       "Headless coding runs require a throwaway sandbox.")
    if not p.is_dir():
        return False, f"REFUSED: '{p}' is not a directory."
    return True, str(p)


def _resolve_cli() -> str | None:
    """The claude CLI to drive headless: the SDK's bundled claude.exe first (proven spawnable), else PATH."""
    try:
        import claude_agent_sdk
        name = "claude.exe" if platform.system() == "Windows" else "claude"
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        if bundled.exists():
            return str(bundled)
    except Exception:
        pass
    import shutil
    return shutil.which("claude")


async def _spawn_claude_p(ws: Path, prompt: str) -> dict:
    """Run `claude -p` headless in the sandbox: acceptEdits + agent's _BUILDER_TOOLS allowlist, JSON
    output, a CLI-side $ budget. Subscription auth ONLY (scrubbed env + CONFIG_DIR, never a key).
    5-min timeout -> PID-tree kill. Captures rc + scrubbed stdout/stderr tails. Never raises."""
    cli = _resolve_cli()
    if not cli:
        return {"spawned": False, "error": "claude CLI not found (bundled or on PATH)."}
    args = [cli, "-p",
            "--permission-mode", "acceptEdits",                # least-dangerous mode (NOT skip-all)
            "--allowedTools", ",".join(agent._BUILDER_TOOLS),  # Read/Write/Edit/Glob/Grep + Bash(git:*)/python/...
            "--output-format", "json",
            "--max-budget-usd", "5"]                           # CLI-side spend bound (with the $5 cap)
    # SUBSCRIPTION AUTH ONLY: a clean child env (scrub keys), CONFIG_DIR set, NEVER an API key.
    env = {k: v for k, v in os.environ.items() if k not in agent._SCRUB_KEYS}
    env["CLAUDE_CONFIG_DIR"] = str(agent.CONFIG_DIR)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(ws), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        return {"spawned": False, "error": f"spawn failed: {type(e).__name__}: {repr(e)[:160]}"}
    pid = proc.pid
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=_HEADLESS_TIMEOUT_S)
        full_out = out.decode("utf-8", "replace")
        full_err = err.decode("utf-8", "replace")
        meta = headless_budget.parse_claude_result(full_out)                      # cost/usage from the JSON result
        rate_limited = headless_budget.detect_rate_limit(proc.returncode, full_out, full_err, meta)
        return {"spawned": True, "pid": pid, "timed_out": False, "rc": proc.returncode,
                "result_meta": meta, "rate_limited": rate_limited,
                "stdout_tail": scrub(full_out)[-1800:], "stderr_tail": scrub(full_err)[-800:]}
    except asyncio.TimeoutError:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)  # PID-tree kill
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return {"spawned": True, "pid": pid, "timed_out": True, "rc": None,
                "result_meta": {}, "rate_limited": False,
                "error": f"headless run exceeded {_HEADLESS_TIMEOUT_S}s — killed PID {pid} (tree)."}


def _git_cross_check(ws: Path, baseline: str) -> dict:
    """The empty-diff honesty check (mirrors orchestrator.propose_result_from_file): did a REAL commit
    land (HEAD moved past the baseline) WITH a non-empty diff? Read-only git via orchestrator helpers."""
    head = orchestrator._git_head(ws)
    committed = bool(head and head != baseline)
    diff_nonempty, subject = False, ""
    if committed:
        if baseline:
            stat = orchestrator._git(ws, "diff", "--shortstat", baseline, head) or ""
        else:                                                  # first commit: diff vs the empty tree
            stat = orchestrator._git(ws, "show", "--format=", "--shortstat", head) or ""
        diff_nonempty = bool(stat.strip())
        subject = orchestrator._git(ws, "log", "-1", "--format=%h %s") or ""
    st = orchestrator._git_state(ws)
    return {"committed": committed, "diff_nonempty": diff_nonempty, "head": (head or "")[:9],
            "baseline": (baseline or "")[:9], "subject": subject, "dirty": st.get("dirty")}


async def run_headless_step(workspace, prompt) -> dict:
    """Run ONE Claude Code step HEADLESS in a disposable sandbox, git-verified. Returns
    {ok, committed, diff_nonempty, summary, error, git, run, ...}. The coding step is ONE real
    subscription Claude call (the point). ok=True ONLY if a real commit with a non-empty diff landed."""
    allowed, info = _check_workspace(workspace)
    if not allowed:
        return {"ok": False, "refused": True, "error": info}
    ws = Path(info)
    if not orchestrator._git_is_repo(ws):
        return {"ok": False, "error": f"REFUSED: '{ws}' is not a git repo — a headless step needs a repo to commit into."}
    block = usage.claude_blocked_reason()                      # $5 cap / free_only backstop (respected, not modified)
    if block:
        return {"ok": False, "blocked": True, "error": f"spend guard blocked the step: {block}"}
    gate = headless_budget.gate_check()                        # SEPARATE run-count/cost gate (fail-closed) — BEFORE any spawn
    if not gate["allowed"]:
        return {"ok": False, "blocked_headless_budget": True, "reason": gate["reason"],
                "count": gate["count"], "cap": gate["cap"],
                "reported_cost": gate["reported_cost"], "ceiling": gate["ceiling"],
                "summary": f"REFUSED (no spawn): {gate['reason']} [{gate['count']}/{gate['cap']} runs today]",
                "spawned": False}
    try:
        hs = ws / orchestrator._HANDSHAKE_DIR                  # write the prompt to <ws>/.nervice/prompt.md (handoff)
        hs.mkdir(parents=True, exist_ok=True)
        (hs / orchestrator._PROMPT_FILE).write_text(prompt, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"couldn't write the prompt file: {type(e).__name__}"}
    baseline = orchestrator._git_head(ws)
    run = await _spawn_claude_p(ws, prompt)
    check = _git_cross_check(ws, baseline)
    ok = bool(run.get("spawned") and not run.get("timed_out") and check["committed"] and check["diff_nonempty"])
    if run.get("timed_out"):
        summary = run.get("error")
    elif check["committed"] and not check["diff_nonempty"]:
        summary = "FLAGGED: a commit landed but its diff is EMPTY — no real change (empty-diff honesty check)."
    elif not check["committed"]:
        summary = ("FLAGGED: NO new commit in the workspace — Claude Code landed no work"
                   + (" (it left uncommitted changes)" if check["dirty"] else "") + ".")
    else:
        summary = f"OK: headless step committed real work — {check['subject']}."
    meta = run.get("result_meta") or {}
    rate_limited = bool(run.get("rate_limited"))
    if rate_limited:
        summary = "PAUSED: subscription rate/usage limit hit during the headless step — not retried. " + summary
    recorded = False
    if run.get("spawned"):                                     # a real attempt consumed quota -> record it (additive)
        recorded = headless_budget.record_run({
            "workspace": str(ws), "ok": ok, "committed": check["committed"],
            "reported_cost": meta.get("reported_cost"),
            "input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
            "turns": meta.get("turns"), "duration_ms": meta.get("duration_ms"),
            "rc": run.get("rc"), "timed_out": run.get("timed_out"), "rate_limited": rate_limited})
    budget = headless_budget.gate_check()                      # post-run snapshot (now includes this run)
    return {"ok": ok, "committed": check["committed"], "diff_nonempty": check["diff_nonempty"],
            "summary": summary, "error": run.get("error"), "workspace": str(ws), "git": check,
            "rate_limited": rate_limited, "result_meta": meta, "ledger_recorded": recorded,
            "budget": {k: budget.get(k) for k in ("count", "cap", "reported_cost", "ceiling", "allowed")},
            "run": {k: run.get(k) for k in ("spawned", "timed_out", "rc", "pid", "stdout_tail", "stderr_tail")}}

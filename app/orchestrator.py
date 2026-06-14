"""Orchestrator v1 — a PLANNER, not an executor.

Nervice reads Nate's project doc (docs/projects/ACTIVE.md), critiques it for STRUCTURAL gaps, breaks
it into an ordered list of small one-component-at-a-time steps, and emits the exact Claude Code
prompt for the CURRENT step as TEXT for Nate to paste into interactive Claude Code (which stays free
post-June-15). v1 EXECUTES NOTHING — no agent_task, no Agent SDK, no screen control, no file writes
beyond its own state. It only reads ACTIVE.md and tracks progress in data/orchestrator_state.json.

FREE RUNGS ONLY: every planner/critic LLM call runs on Groq first, the local Ollama 4B when Groq is
capped, and an honest "need Groq/Ollama" otherwise. It NEVER calls Claude / the Agent SDK — that tier
is now metered (the June-15 Agent-SDK credit). This module deliberately imports no Claude path; the
only `app.agent` import is the `current_rung` telemetry contextvar (state, not a call).
"""
import re
import sys
import json
import hashlib
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from groq import RateLimitError

from app.llm import _client, GROQ_MODEL, groq_capped, _stick_cap   # Groq free tier (no Claude here)
from app import ollama_client as ollama                            # local 4B rung (free, unlimited)
from app.agent import current_rung                                 # telemetry contextvar only

_ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVE_DOC = _ROOT / "docs" / "projects" / "ACTIVE.md"
STATE_FILE = _ROOT / "data" / "orchestrator_state.json"
TZ = ZoneInfo("America/Chicago")

_TEMPLATE = """# PROJECT — <title>

## Goal
<One or two sentences: what this project IS and why. What does "done" look like?>

## Context
<Where it lives, what it builds on, who/what it's for. Constraints (free-only? local-only?).>

## Requirements
- <Concrete, testable requirement>
- <Another — each should have a clear "how do we know it works">

## Out of scope
- <What this project is explicitly NOT doing (yet)>

## Success criteria
- <How we'll know the whole project is done>
"""

# The working style the critic/planner judge the doc against and bake into generated prompts.
# Distilled from docs/PROJECT_STATE.md so the planner stays in Nervice's lane.
_NERVICE_CONTEXT = (
    "Nervice is a local-first personal AI assistant (FastAPI on localhost; a Groq->Ollama->Claude "
    "brain ladder; Kokoro TTS + faster-whisper STT; Windows, one RTX 4060). The working style Nate "
    "insists on, judge against it: FREE-FIRST (no paid/metered service unless truly unavoidable); "
    "ONE COMPONENT AT A TIME (small, independently verifiable changes — never a mass rewrite); REAL "
    "DATA ONLY (never present mocked/placeholder output as real); honest failure over silent fakery; "
    "the server runs committed code and is verified after a restart.")

# A generated step that would touch Nervice's OWN safety/self-mod/core internals is OUT OF SCOPE for a
# project plan — flag it to Nate, never emit it as a ready-to-paste prompt. Deterministic, on the
# generated text (defense in depth; the planner is told to stay in scope too).
_SCOPE_FLAG_RE = re.compile(
    r"\b(safety\.py|selfmod\.py|SAFETY_FLOOR|_SCRUB_KEYS|app[\\/]agent\.py|app[\\/]persona\.py|"
    r"the jail\b|disable[ -](?:the[ -])?(?:safety|guard|jail)|"
    r"nervice'?s own (?:code|safety|persona|self|internals|brain))\b", re.I)

_NEED_FREE_MSG = ("I need the fast model or my local backup brain to plan, and both are unavailable "
                  "right now — Groq's rate-limited and Ollama isn't answering. Try again in a bit.")
_NO_DOC_MSG = ("I set up a project template at docs/projects/ACTIVE.md — fill in the goal, "
               "requirements, and scope, then say \"critique my project\".")
_NO_PLAN_MSG = "No plan yet — say \"plan my project\" and I'll break the doc into steps."


# --------------------------------------------------------------------------- doc + state I/O

def _read_doc() -> str | None:
    """Return the project doc text, or None if it's absent/just-templated/empty (caller nudges Nate
    to fill it). Creates the template on first run so there's always something at the path."""
    if not ACTIVE_DOC.exists():
        try:
            ACTIVE_DOC.parent.mkdir(parents=True, exist_ok=True)
            ACTIVE_DOC.write_text(_TEMPLATE, encoding="utf-8")
        except Exception as e:
            print(f"[orchestrator] could not write template: {repr(e)[:80]}", file=sys.stderr)
        return None
    try:
        text = ACTIVE_DOC.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        print(f"[orchestrator] could not read doc: {repr(e)[:80]}", file=sys.stderr)
        return None
    # Unfilled template (still has the <placeholder> markers and no real content) -> treat as empty.
    if not text or (text.count("<") >= 4 and "PROJECT — <title>" in text):
        return None
    return text


def _doc_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    state["updated"] = datetime.now(TZ).isoformat(timespec="seconds")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[orchestrator] could not save state: {repr(e)[:80]}", file=sys.stderr)


# --------------------------------------------------------------------------- the FREE-only LLM call

async def _ask_free(system: str, user: str, *, want_json: bool = False, num_predict: int = 500):
    """Run ONE planner/critic completion on the FREE rungs only — Groq first, local Ollama when Groq
    is capped, and an honest empty otherwise. NEVER Claude/the Agent SDK. Sets current_rung for the
    turn telemetry and returns (result, rung) where rung is 'groq' | 'ollama' | 'none' and result is
    a dict (want_json) or str."""
    if not groq_capped():
        try:
            kw = {"response_format": {"type": "json_object"}} if want_json else {}
            resp = await _client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.2, max_tokens=num_predict, **kw)
            raw = (resp.choices[0].message.content or "").strip()
            current_rung.set("groq")
            return (json.loads(raw) if want_json else raw), "groq"
        except RateLimitError as e:
            _stick_cap(e)
            print("[orchestrator] groq capped -> local ollama rung", file=sys.stderr)
        except Exception as e:
            print(f"[orchestrator] groq error -> ollama: {repr(e)[:120]}", file=sys.stderr)
    try:
        if want_json:
            obj = await ollama.chat_json(system, user)
            current_rung.set("ollama")
            return obj, "ollama"
        m = await ollama.chat([{"role": "system", "content": system},
                               {"role": "user", "content": user}], options={"num_predict": num_predict})
        out = (m.get("content") or "").strip()
        if out:
            current_rung.set("ollama")
            return out, "ollama"
    except ollama.OllamaUnavailable as e:
        print(f"[orchestrator] ollama unavailable: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[orchestrator] ollama error: {repr(e)[:120]}", file=sys.stderr)
    current_rung.set("exhausted")
    return (None if want_json else ""), "none"


# --------------------------------------------------------------------------- formatting helpers

def _fmt_gaps(gaps: list, voice: bool) -> str:
    if not gaps:
        return "The doc looks structurally solid — I didn't find major gaps. Say \"plan my project\" when you're ready."
    if voice:
        titles = [g.get("title", "a gap") for g in gaps[:3]]
        lead = ", ".join(titles[:-1]) + (f", and {titles[-1]}" if len(titles) > 1 else titles[0])
        more = f" — plus {len(gaps) - 3} more" if len(gaps) > 3 else ""
        return (f"I found {len(gaps)} thing{'s' if len(gaps) != 1 else ''} to tighten up: {lead}{more}. "
                "Say \"show gaps\" for the details.")
    lines = [f"Found {len(gaps)} structural gap{'s' if len(gaps) != 1 else ''} in the doc:\n"]
    for i, g in enumerate(gaps, 1):
        lines.append(f"{i}. {g.get('title', 'gap')} — {g.get('detail', '')}")
    lines.append("\nThese are flags, not blockers. Tighten the doc, then say \"plan my project\".")
    return "\n".join(lines)


def _fmt_plan(steps: list, current: int, voice: bool) -> str:
    n = len(steps)
    if voice:
        first = steps[current]["title"] if current < n else "—"
        return (f"Broke it into {n} step{'s' if n != 1 else ''}, one component at a time. "
                f"Up next: {first}. Say \"next step\" for the prompt to paste.")
    lines = [f"Plan — {n} step{'s' if n != 1 else ''} (one component at a time):\n"]
    for i, s in enumerate(steps):
        mark = "[x]" if s.get("status") == "done" else ("[>]" if i == current else "[ ]")
        lines.append(f"{mark} {i + 1}. {s.get('title', 'step')} — {s.get('detail', '')}")
    lines.append("\nSay \"next step\" for the current step's Claude Code prompt.")
    return "\n".join(lines)


def _prompt_reply(step_idx: int, total: int, title: str, prompt: str, lead: str = "") -> str:
    """The CC prompt, fenced so voice SPEAKS a short pointer ('I've put the code on screen') while the
    HUD/text shows the full prompt for Nate to copy. Same string for voice and text."""
    head = lead or f"Step {step_idx + 1} of {total} — {title}. Paste this into Claude Code:"
    return f"{head}\n\n```\n{prompt.strip()}\n```"


# --------------------------------------------------------------------------- ops

async def _op_critique(voice: bool) -> str:
    doc = _read_doc()
    if doc is None:
        current_rung.set("direct")
        return _NO_DOC_MSG
    system = (
        "You are the planning critic for Nervice. Review Nate's PROJECT DOC for STRUCTURAL gaps ONLY — "
        "do not solve the project, do not write code, do not add features. Flag: missing/vague success "
        "criteria, ambiguous or under-specified requirements, undefined scope (in vs out), hidden "
        "dependencies or ordering issues, and unstated assumptions (data sources, environment, auth, "
        "formats). You FLAG, you never block. Output ONLY JSON: "
        '{"gaps":[{"title":"<5-8 word label>","detail":"<one sentence: the gap and the question Nate '
        'should answer>"}]}. At most 5 gaps, most important first; fewer (or empty) if the doc is solid.'
        "\n\nCONTEXT (judge the doc against Nervice's working style):\n" + _NERVICE_CONTEXT)
    obj, rung = await _ask_free(system, f"PROJECT DOC:\n{doc}", want_json=True, num_predict=700)
    if rung == "none":
        return _NEED_FREE_MSG
    gaps = (obj or {}).get("gaps", []) if isinstance(obj, dict) else []
    gaps = [{"title": str(g.get("title", "")).strip(), "detail": str(g.get("detail", "")).strip()}
            for g in gaps if isinstance(g, dict)][:5]
    state = _load_state()
    state["doc_hash"] = _doc_hash(doc)
    state["gaps"] = gaps
    _save_state(state)
    return _fmt_gaps(gaps, voice)


async def _op_plan(voice: bool) -> str:
    doc = _read_doc()
    if doc is None:
        current_rung.set("direct")
        return _NO_DOC_MSG
    h = _doc_hash(doc)
    state = _load_state()
    # Plan already current for this doc -> just SHOW it (no regen, no LLM call).
    if state.get("steps") and state.get("plan_hash") == h:
        current_rung.set("direct")
        return _fmt_plan(state["steps"], state.get("current", 0), voice)
    system = (
        "You break Nate's PROJECT DOC into an ORDERED list of SMALL, single-component build steps for "
        "interactive Claude Code. Respect: ONE component at a time (no mass rewrites), free-first, real "
        "data only. Each step must be independently buildable AND verifiable. Order by dependency. Do "
        "NOT write code or the Claude Code prompt here — only the step list. Output ONLY JSON: "
        '{"steps":[{"title":"<short imperative label>","detail":"<one sentence: what it builds and how '
        'it is verified>"}]}. Prefer 3 to 8 steps.'
        "\n\nCONTEXT:\n" + _NERVICE_CONTEXT)
    obj, rung = await _ask_free(system, f"PROJECT DOC:\n{doc}", want_json=True, num_predict=900)
    if rung == "none":
        return _NEED_FREE_MSG
    raw = (obj or {}).get("steps", []) if isinstance(obj, dict) else []
    steps = [{"title": str(s.get("title", "")).strip(), "detail": str(s.get("detail", "")).strip(),
              "status": "pending"} for s in raw if isinstance(s, dict) and s.get("title")]
    if not steps:
        return "I couldn't break that into clean steps — the doc may be too thin. Try \"critique my project\" first."
    state["plan_hash"] = h
    state["doc_hash"] = h
    state["steps"] = steps
    state["current"] = 0
    _save_state(state)
    return _fmt_plan(steps, 0, voice)


async def _gen_step_prompt(doc: str, steps: list, idx: int, lead: str = "") -> str:
    """Generate (free rung) the Claude Code prompt for step `idx`, scope-check it, and fence it."""
    step = steps[idx]
    system = (
        "You write ONE precise, terse prompt for Nate to paste into interactive Claude Code to build a "
        "SINGLE step. The prompt must state exactly: the file(s) to create/edit, the exact behavior, "
        "the test/verification, and to \"report what changed and WAIT for review — don't move on.\" "
        "Output ONLY the prompt text Nate will paste — no preamble, no options, no commentary. Keep it "
        "tight (a short paragraph or a few lines). Scope it to THIS step ONLY; never bundle other steps; "
        "never touch Nervice's own safety, self-modification, or core brain code."
        "\n\nCONTEXT:\n" + _NERVICE_CONTEXT)
    user = (f"PROJECT DOC:\n{doc}\n\nTHE STEP ({idx + 1} of {len(steps)}; earlier steps are done): "
            f"{step['title']} — {step.get('detail', '')}")
    prompt, rung = await _ask_free(system, user, want_json=False, num_predict=500)
    if rung == "none":
        return _NEED_FREE_MSG
    # SCOPE GUARD: a step that would touch Nervice's own safety/self-mod/core is out of scope — flag,
    # don't emit. Checks both the model's prompt and the step label.
    if _SCOPE_FLAG_RE.search(prompt) or _SCOPE_FLAG_RE.search(step["title"] + " " + step.get("detail", "")):
        print(f"[orchestrator] SCOPE FLAG on step {idx + 1}: {step['title']!r}", file=sys.stderr)
        return (f"⚠️ Heads up — step {idx + 1} (\"{step['title']}\") looks like it would touch Nervice's "
                "own safety, self-mod, or core code. That's out of scope for a project plan and stays "
                "behind the self-mod approval gate. I'm flagging it instead of handing you a prompt — "
                "re-scope this step to the project itself, or handle that change through the normal "
                "self-update flow.")
    return _prompt_reply(idx, len(steps), step["title"], prompt, lead=lead)


async def _op_next(voice: bool) -> str:
    doc = _read_doc()
    state = _load_state()
    steps = state.get("steps") or []
    if not steps:
        current_rung.set("direct")
        return _NO_PLAN_MSG
    if doc is None:                                  # plan exists but doc went missing
        current_rung.set("direct")
        return _NO_DOC_MSG
    idx = state.get("current", 0)
    if idx >= len(steps):
        current_rung.set("direct")
        return "Every step is marked done — the plan's complete. Update the doc and say \"plan my project\" to start a new pass."
    return await _gen_step_prompt(doc, steps, idx)


async def _op_redo(voice: bool) -> str:
    doc = _read_doc()
    state = _load_state()
    steps = state.get("steps") or []
    if not steps:
        current_rung.set("direct")
        return _NO_PLAN_MSG
    if doc is None:
        current_rung.set("direct")
        return _NO_DOC_MSG
    idx = state.get("current", 0)
    if idx >= len(steps):
        current_rung.set("direct")
        return "Nothing to redo — every step is done."
    return await _gen_step_prompt(doc, steps, idx, lead=f"Another take on step {idx + 1} — {steps[idx]['title']}. Paste this:")


async def _op_done(voice: bool) -> str:
    state = _load_state()
    steps = state.get("steps") or []
    if not steps:
        current_rung.set("direct")
        return _NO_PLAN_MSG
    idx = state.get("current", 0)
    if idx >= len(steps):
        current_rung.set("direct")
        return "All steps were already done."
    steps[idx]["status"] = "done"
    state["current"] = idx + 1
    _save_state(state)
    done_n = idx + 1
    if state["current"] >= len(steps):
        current_rung.set("direct")
        return f"Marked \"{steps[idx]['title']}\" done. That's all {len(steps)} steps complete — nice work."
    # advance + hand over the NEXT step's prompt (LLM op)
    doc = _read_doc()
    if doc is None:
        current_rung.set("direct")
        return f"Marked \"{steps[idx]['title']}\" done — {done_n} of {len(steps)}. (Doc's missing, so I can't write the next prompt.)"
    nxt = state["current"]
    lead = (f"Marked \"{steps[idx]['title']}\" done — {done_n} of {len(steps)}. "
            f"On to step {nxt + 1}, {steps[nxt]['title']}. Paste this:")
    return await _gen_step_prompt(doc, steps, nxt, lead=lead)


async def _op_status(voice: bool) -> str:
    current_rung.set("direct")
    state = _load_state()
    steps = state.get("steps") or []
    if not steps:
        return _NO_PLAN_MSG
    idx = state.get("current", 0)
    done = sum(1 for s in steps if s.get("status") == "done")
    if idx >= len(steps):
        return f"All {len(steps)} steps done. Say \"plan my project\" to start a fresh pass."
    cur = steps[idx]
    if voice:
        return f"You're on step {idx + 1} of {len(steps)}, {cur['title']}. {done} done so far."
    return (f"Step {idx + 1} of {len(steps)} — {cur['title']}: {cur.get('detail', '')}\n"
            f"{done} of {len(steps)} done. Say \"next step\" for the prompt, or \"mark step done\" to advance.")


async def _op_gaps(voice: bool) -> str:
    current_rung.set("direct")
    state = _load_state()
    gaps = state.get("gaps")
    if not gaps:
        return "No critique on file yet — say \"critique my project\" and I'll review the doc for gaps."
    return _fmt_gaps(gaps, voice)


_OPS = {"critique": _op_critique, "plan": _op_plan, "next": _op_next, "done": _op_done,
        "redo": _op_redo, "status": _op_status, "gaps": _op_gaps}


async def handle(op: str, voice_mode: bool = False) -> str:
    """Dispatch an orchestrator op. Unknown op -> status. Every path is read-only + free-rung-only;
    nothing here executes the plan or calls Claude/the Agent SDK."""
    fn = _OPS.get((op or "").strip().lower(), _op_status)
    print(f"[ROUTE: orchestrator/{op}]", file=sys.stderr)
    return await fn(voice_mode)

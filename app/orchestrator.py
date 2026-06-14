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
import time
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


def _parse_goal(doc: str) -> str:
    """Pull the '## Goal' section text from ACTIVE.md (the real lines under it, placeholders dropped)."""
    m = re.search(r"^##\s*Goal\s*$(.*?)(?=^##\s|\Z)", doc or "", re.M | re.S)
    body = (m.group(1) if m else "").strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("<")]
    return " ".join(lines)[:400]


def _doc_text_or_none() -> str | None:
    """Read ACTIVE.md WITHOUT creating a template (read-only; for the snapshot endpoint). None if
    absent or still an unfilled template."""
    try:
        if not ACTIVE_DOC.exists():
            return None
        t = ACTIVE_DOC.read_text(encoding="utf-8", errors="replace").strip()
        if not t or (t.count("<") >= 4 and "PROJECT — <title>" in t):
            return None
        return t
    except Exception:
        return None


def state_snapshot() -> dict:
    """Read-only snapshot for the PROJECT panel + GET /orchestrator/state. No LLM call. Returns the
    goal, critique gaps, the ordered steps with done/current/pending status, the current index, and
    the CACHED current-step CC prompt (generated when Nate asks for the step — never regenerated on a
    panel refresh). Honest: has_doc is False when the doc is missing or an unfilled template."""
    doc = _doc_text_or_none()
    state = _load_state()
    steps = state.get("steps") or []
    current = int(state.get("current", 0))
    prompts = state.get("prompts") or {}
    out_steps = []
    for i, s in enumerate(steps):
        status = "done" if s.get("status") == "done" else ("current" if i == current else "pending")
        out_steps.append({"n": i + 1, "title": s.get("title", ""), "detail": s.get("detail", ""), "status": status})
    return {
        "has_doc": doc is not None,
        "goal": _parse_goal(doc) if doc else "",
        "gaps": state.get("gaps") or [],
        "steps": out_steps,
        "current": current,
        "current_title": steps[current]["title"] if 0 <= current < len(steps) else "",
        "current_prompt": prompts.get(str(current), ""),
        "done_count": sum(1 for s in steps if s.get("status") == "done"),
        "total": len(steps),
        # the plan is stale when steps exist but the doc has changed since they were generated
        "stale": bool(doc is not None and steps and state.get("plan_hash") != _doc_hash(doc)),
        "updated": state.get("updated", ""),
    }


# --------------------------------------------------------------------------- the FREE-only LLM call

def _loads_lenient(text: str):
    """Parse JSON tolerantly — the local 4B sometimes appends prose AFTER the JSON object (a strict
    json.loads then fails with 'Extra data'). Try strict first, then the first balanced {...}/[...].
    Returns the parsed value or None."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"[\{\[].*[\}\]]", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


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
            try:
                obj = await ollama.chat_json(system, user)          # format=json, strict parse
            except ollama.OllamaUnavailable:
                raise
            except Exception:
                # the 4B sometimes appends prose after the object -> re-ask and parse leniently
                m = await ollama.chat([{"role": "system", "content": system + "\nReturn ONLY the JSON object — nothing after it."},
                                       {"role": "user", "content": user}], options={"num_predict": num_predict})
                obj = _loads_lenient(m.get("content") or "")
            if obj is not None:
                current_rung.set("ollama")
                return obj, "ollama"
        else:
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
    # Cache the raw prompt so the PROJECT panel / GET /orchestrator/state can show it without a fresh
    # Groq call on every auto-refresh (regenerated only when Nate asks for the step again).
    cache = _load_state()
    cache.setdefault("prompts", {})[str(idx)] = prompt.strip()
    _save_state(cache)
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


async def _op_summary(voice: bool) -> str:
    """A SHORT, Groq-grounded status update from the real project state (the same data the PROJECT
    panel shows). Free rung only; honest empty; never fabricated progress."""
    snap = state_snapshot()
    if not snap["has_doc"] and not snap["steps"]:
        current_rung.set("direct")
        return _NO_DOC_MSG
    gaps = snap["gaps"]
    ctx = (f"GOAL: {snap['goal'] or '(not stated in the doc)'}\n"
           f"PROGRESS: {snap['done_count']} of {snap['total']} steps done"
           + (f"; current step: {snap['current_title']}" if snap["current_title"] else "; no plan yet")
           + ("\nOPEN GAPS (" + str(len(gaps)) + "): " + "; ".join(g.get("title", "") for g in gaps[:4])
              if gaps else "\nNo open critique gaps."))
    system = ("You are Nervice giving Nate a SHORT, honest status update on his coding project, using "
              "ONLY the real data below. One or two conversational sentences — no lists, no markdown. "
              "Lead with where things stand. NEVER invent progress, steps, or gaps not shown here.")
    ans, rung = await _ask_free(system, ctx, want_json=False, num_predict=180)
    return ans if rung != "none" else _NEED_FREE_MSG


# =============================== NEW-PROJECT guided setup ===============================
# Nate starts a project by CONVERSATION instead of hand-editing docs/projects/ACTIVE.md. "start a new
# project" begins a stateful, ONE-question-at-a-time flow (goal -> context -> requirements ->
# out-of-scope -> confirm) tracked in data/orchestrator_setup.json so it survives across turns. On
# confirm, Groq drafts the ACTIVE.md (deriving success-criteria from the goal) and it's written; an
# existing real project is archived to docs/projects/archive/ first, never silently lost. Free rungs
# only (the draft is Groq -> Ollama -> deterministic fallback); NEVER Claude. Reads/writes only
# docs/projects/ files + the setup state — nothing else.

SETUP_FILE = _ROOT / "data" / "orchestrator_setup.json"
ARCHIVE_DIR = _ROOT / "docs" / "projects" / "archive"
_SETUP_TTL_S = 3600   # a forgotten setup expires after an hour (Nate can always say "cancel")
_STEP_ORDER = ["goal", "context", "requirements", "out_of_scope"]
_OPTIONAL = {"context", "out_of_scope"}
_Q = {
    "goal": "Let's set up a new project. In a sentence or two — what's the goal? What are you building?",
    "context": "Got it. Any context I should know — where it lives, what it builds on, or any constraints? Say \"skip\" if there's nothing.",
    "requirements": "What are the key requirements — the main things it needs to do? List the big ones.",
    "out_of_scope": "Last thing: anything explicitly OUT of scope, that you're NOT doing yet? Say \"skip\" if none.",
}
_CANCEL_RE = re.compile(r"^\s*(cancel(?: it| this| setup| that)?|never ?mind|forget it|abort|quit|stop(?: it)?)\s*[.!]*\s*$", re.I)
_SKIP_RE = re.compile(r"^\s*(skip(?: it| this)?|none|n/?a|no(?:ne| thanks)?|nope|nah|pass|move on|nothing)\s*[.!]*\s*$", re.I)
_YES_RE = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|looks good|sounds good|do it|save(?: it| that)?|confirm(?: it)?|go ahead|perfect|great|good|that'?s (?:good|right|it)|ship it)\b", re.I)
_REVISE_RE = re.compile(r"\b(change|edit|redo|fix|update|revise|rewrite|tweak|wrong|different|not right)\b", re.I)


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _blank_answers() -> dict:
    return {"goal": "", "context": "", "requirements": "", "out_of_scope": ""}


def _load_setup() -> dict:
    try:
        return json.loads(SETUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_setup(s: dict) -> None:
    try:
        SETUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETUP_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[orchestrator] could not save setup: {repr(e)[:80]}", file=sys.stderr)


def _clear_setup() -> None:
    try:
        SETUP_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[orchestrator] could not clear setup: {repr(e)[:80]}", file=sys.stderr)


def setup_active() -> bool:
    """True if a new-project setup is in progress (and not stale). Checked at the top of every turn —
    must never raise. A setup older than the TTL is cleared and treated as inactive."""
    s = _load_setup()
    if not s.get("active"):
        return False
    try:
        started = datetime.fromisoformat(s.get("started", ""))
        if (datetime.now(TZ) - started).total_seconds() > _SETUP_TTL_S:
            _clear_setup()
            return False
    except Exception:
        pass
    return True


def _next_step(step: str) -> str:
    i = _STEP_ORDER.index(step)
    return _STEP_ORDER[i + 1] if i + 1 < len(_STEP_ORDER) else "confirm"


def _which_field(text: str) -> str | None:
    t = (text or "").lower()
    if "out of scope" in t or "out-of-scope" in t or ("scope" in t and "out" in t):
        return "out_of_scope"
    if "requirement" in t:
        return "requirements"
    if "context" in t or "background" in t:
        return "context"
    if "goal" in t:
        return "goal"
    return None


def _archive_existing() -> str | None:
    """Archive the current real ACTIVE.md to docs/projects/archive/<ts>.md so a project is never lost
    on overwrite. Returns the archive path, or None if there was nothing real to archive."""
    existing = _doc_text_or_none()
    if not existing:
        return None
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = ARCHIVE_DIR / (datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + ".md")
        dest.write_text(existing + "\n", encoding="utf-8")
        print(f"[orchestrator] archived old project -> {dest.name}", file=sys.stderr)
        return str(dest)
    except Exception as e:
        print(f"[orchestrator] archive failed: {repr(e)[:80]}", file=sys.stderr)
        return None


def _assemble_doc(a: dict) -> str:
    """Deterministic ACTIVE.md from the raw answers — the fallback when both free LLMs are down, so a
    new project can still be created (just without LLM-polished success criteria)."""
    def bullets(s):
        items = [ln.strip("-•* \t") for ln in re.split(r"[\n;]+", s or "") if ln.strip("-•* \t")]
        return "\n".join(f"- {it}" for it in items) if items else "- (none specified)"
    goal = (a.get("goal") or "").strip() or "(not specified)"
    ctx = (a.get("context") or "").strip() or "(none specified)"
    title = " ".join(goal.split()[:6])
    return (f"# PROJECT — {title}\n\n## Goal\n{goal}\n\n## Context\n{ctx}\n\n"
            f"## Requirements\n{bullets(a.get('requirements'))}\n\n"
            f"## Out of scope\n{bullets(a.get('out_of_scope'))}\n\n"
            f"## Success criteria\n- The goal above is met and each requirement is built and verified.\n")


async def _draft_doc(answers: dict) -> tuple[str, str]:
    """Groq drafts a clean ACTIVE.md from the answers (deriving success criteria from the goal). Free
    rung; deterministic assembly if both LLMs are down. Returns (markdown, rung). Never Claude."""
    system = (
        "You format Nate's answers into a clean project doc for Nervice's planner. Output ONLY the "
        "markdown, with EXACTLY these sections in order: a '# PROJECT — <short title>' heading, then "
        "'## Goal', '## Context', '## Requirements' (as '- ' bullets), '## Out of scope' (as '- ' "
        "bullets), and '## Success criteria' (1 to 3 concrete, testable '- ' bullets you DERIVE from "
        "the goal and requirements). Use ONLY his answers — never invent features or scope. If a field "
        "was skipped, write '- (none specified)'. Keep it tight and faithful.")
    user = (f"GOAL: {answers.get('goal','')}\n"
            f"CONTEXT: {answers.get('context','') or '(skipped)'}\n"
            f"REQUIREMENTS: {answers.get('requirements','')}\n"
            f"OUT OF SCOPE: {answers.get('out_of_scope','') or '(skipped)'}")
    doc, rung = await _ask_free(system, user, want_json=False, num_predict=700)
    if rung == "none" or not (doc or "").strip():
        current_rung.set("direct")
        return _assemble_doc(answers), "direct"
    return doc.strip(), rung


def _write_active(doc: str) -> str | None:
    """Archive any existing real doc, then write the new ACTIVE.md and CLEAR the old plan state so the
    new project starts fresh. Returns the archive path (or None)."""
    archived = _archive_existing()
    try:
        ACTIVE_DOC.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_DOC.write_text(doc.strip() + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[orchestrator] could not write ACTIVE.md: {repr(e)[:80]}", file=sys.stderr)
    try:
        STATE_FILE.unlink()   # new project -> drop the previous plan / gaps / prompts
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return archived


async def _confirm_reply(s: dict) -> str:
    """Draft the doc (Groq), stash it on the setup state, and ask Nate to confirm. The draft is shown
    in a fenced block so on voice it is PUT ON SCREEN, not read aloud, while a short line is spoken."""
    doc, _rung = await _draft_doc(s["answers"])
    s["draft"] = doc
    _save_setup(s)
    return ("Here's the draft of your project doc:\n\n```\n" + doc.strip() + "\n```\n\n"
            "Say \"yes\" to save it, or tell me what to change — goal, context, requirements, or out-of-scope.")


async def begin_setup(voice_mode: bool = False) -> str:
    """Start the guided new-project flow. If a real project already exists, confirm the replace first
    (it will be archived). No LLM here — rung=direct."""
    current_rung.set("direct")
    existing = _doc_text_or_none()
    if existing:
        goal = _parse_goal(existing) or "your current project"
        _save_setup({"active": True, "step": "overwrite_confirm", "answers": _blank_answers(),
                     "revising": False, "started": _now_iso()})
        return (f"Heads up — you've already got a project: {goal[:140]}. Starting a new one replaces it "
                "(I'll archive the old one first so it's not lost). Want to go ahead? Say \"yes\" to "
                "continue, or \"cancel\" to keep what you've got.")
    _save_setup({"active": True, "step": "goal", "answers": _blank_answers(),
                 "revising": False, "started": _now_iso()})
    return _Q["goal"]


async def setup_continue(user_message: str, voice_mode: bool = False) -> str:
    """Process Nate's answer for the current setup step and advance — one question at a time. Called
    by the in-turn setup gate (chat.respond / streaming.stream_reply) while setup is active. Free
    rungs only (the Groq draft at confirm); NEVER Claude. Sets current_rung for the turn telemetry."""
    current_rung.set("direct")
    s = _load_setup()
    if not s.get("active"):
        return "There's no project setup in progress — say \"start a new project\" to begin one."
    text = (user_message or "").strip()
    step = s.get("step", "goal")

    if step == "overwrite_confirm":
        if _YES_RE.match(text):
            s["step"] = "goal"
            _save_setup(s)
            return _Q["goal"]
        _clear_setup()
        return "Okay, keeping your current project — nothing changed."

    if step in _STEP_ORDER:
        if _CANCEL_RE.match(text):
            _clear_setup()
            return "Cancelled — no project created."
        if _SKIP_RE.match(text):
            if step not in _OPTIONAL:
                return "I need at least this one. " + _Q[step]
            s["answers"][step] = ""
        elif not text:
            return _Q[step]
        else:
            s["answers"][step] = text
        if s.get("revising"):
            s["revising"] = False
            s["step"] = "confirm"
            _save_setup(s)
            return await _confirm_reply(s)
        nxt = _next_step(step)
        s["step"] = nxt
        _save_setup(s)
        return await _confirm_reply(s) if nxt == "confirm" else _Q[nxt]

    if step == "confirm":
        field = _which_field(text)
        if field or _REVISE_RE.search(text):
            if field:
                s["revising"] = True
                s["step"] = field
                _save_setup(s)
                return "Sure — " + _Q[field]
            return "Which part should I change — the goal, context, requirements, or out-of-scope?"
        if _CANCEL_RE.match(text):
            _clear_setup()
            return "Cancelled — nothing saved."
        if _YES_RE.match(text):
            doc = s.get("draft") or _assemble_doc(s["answers"])
            archived = _write_active(doc)
            _clear_setup()
            extra = " Your old project is archived in docs/projects/archive." if archived else ""
            return ("Saved it to docs/projects/ACTIVE.md." + extra +
                    " Say \"critique my project\" and I'll look for gaps, then \"plan my project\" to break it into steps.")
        return "Say \"yes\" to save it, or tell me what to change — goal, context, requirements, or out-of-scope."

    _clear_setup()
    return "Something got tangled in the setup — say \"start a new project\" to try again."


async def _op_new(voice: bool) -> str:
    return await begin_setup(voice)


# =============================== TALK-TO-EDIT the project ===============================
# Nate edits the active project by conversation — "change the goal to X", "add a step for X",
# "remove step 2", "reorder: step 3 before step 1", "rewrite requirement 1 as X". Groq (Ollama
# fallback, lenient JSON) parses the utterance into a STRUCTURED operation on either the DOC
# (ACTIVE.md sections) or the PLAN (steps in orchestrator_state.json); Nervice shows before->after and
# WRITES only on Nate's confirm. Editing a DOC field stales the auto-plan -> PROPOSE a re-plan (never
# silent). Editing a STEP is a direct plan edit. Writes touch only docs/projects/ + the state file;
# anything implying Nervice's own code is scope-flagged, not applied. NEVER Claude.

_PENDING: dict = {}       # in-process pending edit/replan confirm (single user); TTL-bounded
_EDIT_TTL_S = 600
_NO_RE = re.compile(r"^\s*(no|nope|nah|don'?t|do not|cancel|never ?mind|forget it|scrap(?: it| that)?|discard)\s*[.!]*\s*$", re.I)
_DOC_TARGETS = {"goal", "context", "requirements", "out_of_scope"}
_SECTION_NAME = {"goal": "Goal", "context": "Context", "requirements": "Requirements", "out_of_scope": "Out of scope"}

_EDIT_PARSE_SYSTEM = (
    "You convert Nate's spoken project EDIT into a STRUCTURED operation. Output ONLY JSON, no prose. "
    "You are given the current doc + numbered step list; resolve references (\"requirement 2\", "
    "\"step 3\", \"the X step\") to real 1-based indices. JSON shape: "
    '{"surface":"doc|plan","target":"goal|context|requirements|out_of_scope|step",'
    '"op":"set|add|remove|reorder|edit","index":<1-based int|null>,"index2":<1-based int|null>,'
    '"position":"before|after|end|null","value":"<new text; for a step use \'Title :: one-line detail\'>"}. '
    "Examples: \"change the goal to X\" -> doc/goal/set value X. \"rewrite requirement 2 as X\" -> "
    "doc/requirements/edit index 2 value X. \"add a requirement X\" -> doc/requirements/add value X. "
    "\"add a step for X\" -> plan/step/add value 'X :: <detail>' position end. \"remove step 2\" -> "
    "plan/step/remove index 2. \"reorder: step 3 before step 1\" -> plan/step/reorder index 3 index2 1 "
    "position before. \"change step 2 to X\" -> plan/step/edit index 2 value 'X :: <detail>'. Use ONLY "
    "Nate's instruction; never invent unrelated changes. If it isn't a clear edit, set op to \"unclear\".")


def _get_section(doc: str, name: str) -> str:
    m = re.search(rf"^##\s*{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", doc or "", re.M | re.S)
    return (m.group(1).strip() if m else "")


def _set_section(doc: str, name: str, body: str) -> str:
    pat = re.compile(rf"(^##\s*{re.escape(name)}\s*$)(.*?)(?=^##\s|\Z)", re.M | re.S)
    if pat.search(doc or ""):
        return pat.sub(lambda m: m.group(1) + "\n" + body.strip() + "\n\n", doc, count=1)
    return (doc or "").rstrip() + f"\n\n## {name}\n{body.strip()}\n"


def _bullets(body: str) -> list[str]:
    return [re.sub(r"^[-*•]\s*", "", ln).strip() for ln in (body or "").splitlines()
            if ln.strip().startswith(("-", "*", "•"))]


def _bullets_body(items: list[str]) -> str:
    return "\n".join(f"- {b}" for b in items) if items else "- (none specified)"


def _split_step(value: str) -> tuple[str, str]:
    if "::" in value:
        t, d = value.split("::", 1)
        return t.strip(), d.strip()
    return value.strip(), ""


def _edit_context(doc: str | None, steps: list) -> str:
    parts = ["CURRENT DOC:\n" + (doc[:1500] if doc else "(no doc)")]
    parts.append("\nCURRENT STEPS:")
    parts.append("\n".join(f"{i + 1}. {s.get('title','')}" for i, s in enumerate(steps)) if steps else "(no plan yet)")
    return "\n".join(parts)


def _coerce_edit(obj) -> dict | None:
    if not isinstance(obj, dict):
        return None
    surface, op = str(obj.get("surface", "")).lower(), str(obj.get("op", "")).lower()
    if surface not in ("doc", "plan") or op in ("", "unclear"):
        return None
    d = {"surface": surface, "op": op, "target": str(obj.get("target", "")).lower(),
         "value": str(obj.get("value", "") or "").strip(),
         "position": (str(obj.get("position", "") or "").lower() or None)}
    for k in ("index", "index2"):
        v = obj.get(k)
        try:
            d[k] = int(v) if v not in (None, "", "null") else None
        except Exception:
            d[k] = None
    return d


def _build_proposal(op: dict, doc: str | None, state: dict, steps: list) -> dict:
    """Compute before->after + the exact write payload for an edit. Returns {summary, summary_done,
    apply} or {error}. Pure/deterministic — no LLM, no writes."""
    surface, kind, target, value = op["surface"], op["op"], op.get("target", ""), op.get("value", "")
    idx, idx2 = op.get("index"), op.get("index2")

    if surface == "doc":
        if doc is None:
            return {"error": "There's no project doc to edit yet — say \"start a new project\" first."}
        if target not in _DOC_TARGETS:
            return {"error": "I can edit the goal, context, requirements, or out-of-scope — which one?"}
        name = _SECTION_NAME[target]
        if target in ("goal", "context"):
            before = _get_section(doc, name)
            new_doc = _set_section(doc, name, value)
            return {"summary": f"{name} — before: \"{before[:90]}\" → after: \"{value[:90]}\"",
                    "summary_done": f"{name} updated.", "apply": {"type": "doc", "doc_text": new_doc}}
        items = _bullets(_get_section(doc, name))   # requirements / out_of_scope (a bullet list)
        if kind == "add":
            items = items + [value]
            new_doc = _set_section(doc, name, _bullets_body(items))
            return {"summary": f"Add to {name}: \"{value[:100]}\"", "summary_done": f"Added to {name}.",
                    "apply": {"type": "doc", "doc_text": new_doc}}
        if idx is None or not (1 <= idx <= len(items)):
            return {"error": f"There's no {name.lower()} item {idx} — there {'are' if len(items)!=1 else 'is'} {len(items)}."}
        if kind == "remove":
            removed = items.pop(idx - 1)
            new_doc = _set_section(doc, name, _bullets_body(items))
            return {"summary": f"Remove {name} {idx}: \"{removed[:100]}\"", "summary_done": f"Removed {name.lower()} {idx}.",
                    "apply": {"type": "doc", "doc_text": new_doc}}
        before = items[idx - 1]                      # edit / rewrite a bullet
        items[idx - 1] = value
        new_doc = _set_section(doc, name, _bullets_body(items))
        return {"summary": f"{name} {idx} — before: \"{before[:80]}\" → after: \"{value[:80]}\"",
                "summary_done": f"{name} {idx} rewritten.", "apply": {"type": "doc", "doc_text": new_doc}}

    # surface == "plan" (steps)
    if not steps and kind != "add":
        return {"error": "There's no plan yet — say \"plan my project\" first."}
    current = int(state.get("current", 0))
    cur_obj = steps[current] if 0 <= current < len(steps) else None
    if kind == "add":
        title, detail = _split_step(value)
        if not title:
            return {"error": "What should the new step be? Try \"add a step for writing unit tests\"."}
        new_steps = list(steps)
        new_steps.append({"title": title, "detail": detail, "status": "pending"})
        return {"summary": f"Add step: \"{title}\"" + (f" — {detail}" if detail else ""),
                "summary_done": f"Added a step: \"{title}\".",
                "apply": {"type": "plan", "steps": new_steps, "current": current}}
    if idx is None or not (1 <= idx <= len(steps)):
        return {"error": f"There's no step {idx} — there {'are' if len(steps)!=1 else 'is'} {len(steps)}."}
    if kind == "remove":
        removed = steps[idx - 1]
        new_steps = [s for k, s in enumerate(steps) if k != idx - 1]
        if cur_obj is not None and any(s is cur_obj for s in new_steps):
            new_current = next(k for k, s in enumerate(new_steps) if s is cur_obj)
        else:
            new_current = min(idx - 1, len(new_steps))      # removed the current step -> point at the next
        new_current = max(0, min(new_current, len(new_steps)))
        return {"summary": f"Remove step {idx}: \"{removed.get('title','')}\"?",
                "summary_done": f"Removed step {idx}.",
                "apply": {"type": "plan", "steps": new_steps, "current": new_current}}
    if kind == "reorder":
        if idx2 is None or not (1 <= idx2 <= len(steps)):
            return {"error": "Reorder needs two steps — e.g. \"reorder: step 3 before step 1\"."}
        ref_obj = steps[idx2 - 1]
        new_steps = list(steps)
        moved = new_steps.pop(idx - 1)
        ref_pos = next((k for k, s in enumerate(new_steps) if s is ref_obj), len(new_steps))
        j = ref_pos if op.get("position") != "after" else ref_pos + 1
        new_steps.insert(j, moved)
        new_current = next((k for k, s in enumerate(new_steps) if s is cur_obj), current) if cur_obj is not None else current
        pos = op.get("position") or "before"
        return {"summary": f"Move step {idx} (\"{moved.get('title','')}\") {pos} step {idx2}.",
                "summary_done": "Reordered the steps.",
                "apply": {"type": "plan", "steps": new_steps, "current": new_current}}
    # edit / rewrite a step
    title, detail = _split_step(value)
    before = steps[idx - 1].get("title", "")
    new_steps = list(steps)
    new_steps[idx - 1] = {"title": title or before, "detail": detail or steps[idx - 1].get("detail", ""),
                          "status": steps[idx - 1].get("status", "pending")}
    return {"summary": f"Step {idx} — before: \"{before[:80]}\" → after: \"{(title or before)[:80]}\"",
            "summary_done": f"Step {idx} updated.",
            "apply": {"type": "plan", "steps": new_steps, "current": current}}


async def propose_edit(user_message: str, voice_mode: bool = False) -> str:
    """Parse a spoken edit (Groq), compute before->after, and ask Nate to confirm before writing.
    Free rungs only; scope-flags anything touching Nervice's own code. rung set for telemetry."""
    global _PENDING
    current_rung.set("direct")
    doc = _doc_text_or_none()
    state = _load_state()
    steps = state.get("steps") or []
    if doc is None and not steps:
        return "There's no active project to edit — say \"start a new project\" to set one up."
    if _SCOPE_FLAG_RE.search(user_message or ""):
        return ("⚠️ That looks like it'd touch Nervice's own code or safety — out of scope for a project "
                "edit. I only edit the project doc and its plan.")
    obj, rung = await _ask_free(_EDIT_PARSE_SYSTEM, _edit_context(doc, steps) + "\n\nEDIT: " + (user_message or ""),
                               want_json=True, num_predict=400)
    if rung == "none":
        return _NEED_FREE_MSG
    op = _coerce_edit(obj)
    if not op:
        return ("I couldn't turn that into a clear edit. Try \"change the goal to ...\", \"add a step "
                "for ...\", \"remove step 2\", \"reorder step 3 before step 1\", or \"rewrite requirement 1 as ...\".")
    if _SCOPE_FLAG_RE.search(op.get("value", "")):
        return "⚠️ That edit's content looks like it'd touch Nervice's own code/safety — flagging it instead of applying."
    prop = _build_proposal(op, doc, state, steps)
    if prop.get("error"):
        return prop["error"]
    _PENDING = {"kind": "edit", "summary_done": prop["summary_done"], "apply": prop["apply"], "ts": time.time()}
    return prop["summary"] + "\n\nSave this? (yes / no)"


async def _apply_edit(pending: dict) -> str:
    """Write a confirmed edit. DOC writes then PROPOSE a re-plan if the auto-plan is now stale (never
    silent). PLAN writes are direct. Only docs/projects/ + the state file are touched."""
    global _PENDING
    current_rung.set("direct")
    apply = pending["apply"]
    done = pending.get("summary_done", "Done.")
    if apply["type"] == "doc":
        try:
            ACTIVE_DOC.parent.mkdir(parents=True, exist_ok=True)
            ACTIVE_DOC.write_text(apply["doc_text"].strip() + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[orchestrator] edit write failed: {repr(e)[:80]}", file=sys.stderr)
            _PENDING = {}
            return "I couldn't write that change to the doc — try again."
        state = _load_state()
        steps = state.get("steps") or []
        new_hash = _doc_hash(apply["doc_text"])
        # plan-staleness: a DOC change makes the auto-generated plan stale -> PROPOSE a re-plan.
        if steps and state.get("plan_hash") != new_hash:
            _PENDING = {"kind": "replan", "ts": time.time()}
            return (done + f" Heads up — your {len(steps)}-step plan was built from the old doc, so it's "
                    "now out of date. Want me to re-plan? (yes / no)")
        _PENDING = {}
        return done
    # plan edit (direct)
    state = _load_state()
    state["steps"] = apply["steps"]
    state["current"] = int(apply.get("current", 0))
    _save_state(state)
    _PENDING = {}
    return done


async def resolve_edit(user_message: str) -> str | None:
    """In-turn gate: a pending edit/replan confirm consumes the next yes/no. Returns the reply, or
    None when there's no pending (let routing proceed). A non-yes/no message drops the stale pending
    and returns None so the turn routes fresh. Checked in chat.respond / streaming.stream_reply."""
    global _PENDING
    if not _PENDING:
        return None
    if time.time() - _PENDING.get("ts", 0) > _EDIT_TTL_S:
        _PENDING = {}
        return None
    text = (user_message or "").strip()
    kind = _PENDING.get("kind")
    if _NO_RE.match(text):
        _PENDING = {}
        current_rung.set("direct")
        return ("Okay — scrapped that, nothing changed." if kind == "edit"
                else "Okay, keeping the current plan (it's out of date with the doc until you re-plan).")
    if _YES_RE.match(text):
        if kind == "edit":
            return await _apply_edit(_PENDING)        # may set a replan-confirm pending
        if kind == "replan":
            _PENDING = {}
            return await _op_plan(False)              # regenerate from the new doc (sets rung groq/ollama)
    _PENDING = {}                                     # unrelated message -> drop the stale pending, route fresh
    return None


_OPS = {"critique": _op_critique, "plan": _op_plan, "next": _op_next, "done": _op_done,
        "redo": _op_redo, "status": _op_status, "gaps": _op_gaps, "summary": _op_summary,
        "new": _op_new}


async def handle(op: str, user_message: str = "", voice_mode: bool = False) -> str:
    """Dispatch an orchestrator op. The 'edit' op needs the full utterance; the rest take just
    voice_mode. Unknown op -> status. Free-rung-only; never Claude/the Agent SDK."""
    op = (op or "").strip().lower()
    print(f"[ROUTE: orchestrator/{op}]", file=sys.stderr)
    if op == "edit":
        return await propose_edit(user_message, voice_mode)
    return await _OPS.get(op, _op_status)(voice_mode)

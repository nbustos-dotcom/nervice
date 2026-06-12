import re
import sys
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.persona import PERSONA
from app.retrieval import retrieve
from app.agent import current_rung
from app.turnlog import log_turn
from app.llm import chat_stream, chat_with_tools, TOOL_TIMEOUTS, TIMEOUT_MSG, LADDER_EXHAUSTED_MSG
from app.router import classify
from app.tools import TOOLS, TOOL_FUNCS
from app import sysinfo
from app.memory import remember
from app.weather import get_weather
from app import computer
from app import skills
from app import music
from app import confusion
from app.db import AsyncSessionLocal
from app.models import Message

TZ = ZoneInfo("America/Chicago")


def _format_memories(r, cap_chars: int = 1600):
    """Memory block with a hard size cap (Fix 2.2): ~400 tokens (1600 chars) for the Groq rung,
    ~150 tokens (600 chars) for the compact local-rung block. Essentials first, then topical —
    the cap trims from the least-relevant end."""
    items = r["core"] + r["topic"]
    lines, used = [], 0
    for m in items:
        ln = f"- [{m.category}] {m.content}"
        if used + len(ln) > cap_chars:
            break
        lines.append(ln)
        used += len(ln) + 1
    return "\n".join(lines) if lines else "(nothing stored yet)"


# Compact memory block for the LOCAL rung, stashed by build_system_prompt. Module state (not a
# contextvar) on purpose: build_system_prompt runs inside asyncio.gather — a CHILD task — so a
# contextvar set there would never reach the parent turn. respond()/stream_reply() call
# apply_local_memory() right after the gather to publish it into the turn's context (Fix 2.2).
_pending_local_mem = ""


def apply_local_memory():
    from app.llm import local_memory_block
    local_memory_block.set(_pending_local_mem)


VOICE_ADDENDUM = ("VOICE MODE: your reply will be spoken aloud. Flowing conversational sentences "
                  "only — never bullet points, numbered lists, markdown, or headers. "
                  "Lead with the answer and give the SHORTEST complete reply — one or two sentences "
                  "is ideal; 2-4 is the ceiling, not a target, so don't pad to fill it. Go longer "
                  "only when Nate explicitly asks to go deep.")


async def build_system_prompt(user_id, user_message, voice_mode: bool = False):
    global _pending_local_mem
    r = await retrieve(user_id, user_message)
    now = datetime.now(TZ).strftime("%A, %B %d, %Y at %I:%M %p")
    compact = _format_memories({"core": r["core"], "topic": r["topic"][:3]}, cap_chars=600)
    _pending_local_mem = ("" if compact == "(nothing stored yet)" else
                          "WHAT YOU KNOW ABOUT NATE (context only — never volunteer):\n" + compact)
    base = f"{PERSONA}\n\nWHAT YOU KNOW ABOUT NATE:\n{_format_memories(r)}\n\nCURRENT TIME: {now}"
    return f"{base}\n\n{VOICE_ADDENDUM}" if voice_mode else base


_FORCE = {"hard": "consult_claude", "build": "agent_build",
          "selfmod": "propose_self_update", "browse": "browse"}
# spoken/printed the moment a slow forced tool starts, so Nate isn't left waiting in silence
_ACK = {"hard": "Let me think hard about that one — give me a minute.",
        "build": "On it — building now, give me a couple minutes.",
        "browse": "Opening it up — one sec.",
        "selfmod": "Let me draft that change for your approval — about a minute."}
# per-route ceilings derived from the single per-tool map in app/llm.py (browse=120 etc.) so the
# forced path here and the auto-fired path in chat_with_tools can never drift apart
_TIMEOUTS = {route: TOOL_TIMEOUTS[tool] for route, tool in _FORCE.items()}
_TIMEOUT_MSG = TIMEOUT_MSG


async def execute_route(user_id, system, route, user_message, window, voice_mode: bool = False, on_ack=None):
    """The post-classify half of a turn. `route` is the router's dict ({"route": name, ...args})
    or a bare string for legacy callers. The LLM router decided INTENT; everything here only
    EXECUTES — risk gating, whitelists, and the pending-confirm gate are unchanged in their
    modules. Shared by respond() and app/streaming.py. on_ack(text) is awaited right before a
    slow forced tool starts."""
    rd = route if isinstance(route, dict) else {"route": route}
    r = rd.get("route", "normal")
    if r == "control":
        # Structured {action, target} from the router when present; computer._decision_from_args
        # applies the SAME risk rules, and interpret(raw) remains the fallback (keyword net).
        print(f"[ROUTE: control -> {rd.get('action') or 'interpret'}]", file=sys.stderr)
        return computer.handle_control(user_id, user_message,
                                       action=rd.get("action"), target=rd.get("target"))
    if r == "system":
        ans = await asyncio.to_thread(sysinfo.answer_by_question,
                                      rd.get("question"), rd.get("path"), user_message)
        if ans is None:
            ans = await asyncio.to_thread(sysinfo.answer_machine_question, user_message)
        if ans:
            print("[ROUTE: system -> direct telemetry]", file=sys.stderr)
            current_rung.set("direct")
            return ans
        r = "normal"                      # unmappable machine question -> normal pipeline
    if r == "skill":
        print(f"[ROUTE: skill/{rd.get('op','run')}]", file=sys.stderr)
        current_rung.set("skill")
        return await skills.execute_op(user_id, rd.get("op", "run"), rd.get("trigger", ""), user_message)
    if r == "music_mgmt":
        print(f"[ROUTE: music_mgmt/{rd.get('op')}]", file=sys.stderr)
        current_rung.set("direct")
        return music.apply(rd.get("op"), rd.get("artists"))
    force = _FORCE.get(r)
    if force:
        print(f"[ROUTE: {r} -> {force}]", file=sys.stderr)
        if on_ack:
            await on_ack(_ACK[r])        # immediate feedback before the slow tool
    coro = chat_with_tools(system, window + [{"role": "user", "content": user_message}],
                           TOOLS, TOOL_FUNCS, force_tool=force, voice_mode=voice_mode)
    if force:
        try:
            # route maps 1:1 to the forced agent tool, so this applies that tool's timeout;
            # wait_for cancels chat_with_tools (and the awaited agent) cleanly on expiry
            return await asyncio.wait_for(coro, _TIMEOUTS[r])
        except asyncio.TimeoutError:
            return _TIMEOUT_MSG
    return await coro


async def respond(user_id, user_message, window, voice_mode: bool = False, speak=None):
    t0 = time.monotonic()
    current_rung.set("groq")             # reset per turn; ask_claude flips it on a Claude escalation
    # A pending local-action confirmation takes precedence over routing: a "yes"/"no" here answers
    # the prior RISKY ask, never gets classified as a fresh turn.
    pending = computer.resolve_pending(user_id, user_message)
    if pending is not None:
        print(pending)
        log_turn("control", "control", time.monotonic() - t0, "rest")
        return pending

    # skill SAVE confirmation (a deterministic yes/no, sibling of the control pending gate)
    sk_pending = skills._resolve_pending(user_id, user_message)
    if sk_pending is not None:
        print(sk_pending)
        log_turn("skill", "skill", time.monotonic() - t0, "rest")
        return sk_pending

    # EXACT-normalized saved-trigger match ONLY (string-equal — nothing fuzzy). Everything else
    # goes to the LLM router, the single intent decider.
    sk = skills.find_skill(user_message)
    if sk is not None:
        reply = await skills.run_skill(user_id, sk)
        print(reply)
        log_turn("skill", "skill", time.monotonic() - t0, "rest")
        return reply

    # memory retrieval and router classification are independent — overlap them so the turn
    # pays max(retrieve, classify) instead of the sum (classify alone measured 0.3-0.8s)
    system, route = await asyncio.gather(
        build_system_prompt(user_id, user_message, voice_mode=voice_mode),
        classify(user_message))
    apply_local_memory()                 # publish the compact local-rung memory block (Fix 2.2)

    # Fix 2.4: deterministic confusion signals. Level 1 = hint in the system prompt;
    # level 2 (consecutive) = this turn goes to consult_claude regardless of topic.
    conf = confusion.check(user_id, user_message)
    if conf == "hint":
        system += confusion.HINT
    elif conf == "escalate":
        route = {"route": "hard"}

    async def _ack(text):
        print(text)                      # immediate text feedback before the slow tool
        if voice_mode and speak:
            speak(text)                  # voice: spoken immediately, before the await

    from app.llm import guard_tripped
    guard_tripped.set(False)             # fresh per turn; the 4B guard sets it on a trip
    reply = await execute_route(user_id, system, route, user_message, window,
                                voice_mode=voice_mode, on_ack=_ack)
    print(reply)
    rung = "exhausted" if (reply or "").startswith(LADDER_EXHAUSTED_MSG) else current_rung.get()
    log_turn(route.get("route", "?"), rung, time.monotonic() - t0, "rest",
             extra=("reason=confusion" if conf == "escalate" else ""))
    confusion.note_turn_end(user_id, rung, guard_tripped.get())
    return reply


GREETING_INSTRUCTION = (
    "Generate Nervice's opening greeting as Nate starts a session. One to two sentences, warm, "
    "natural, time-appropriate (morning/afternoon/evening), mention the weather only if provided, "
    "optionally reference one thing you know about him. No bullet lists.")


async def greeting(user_id, voice_mode: bool = False) -> str:
    r = await retrieve(user_id, "Nate today")
    now = datetime.now(TZ).strftime("%A, %B %d, %Y at %I:%M %p")
    weather = await get_weather()
    weather_line = f"WEATHER: {weather}" if weather else "WEATHER: (unavailable — do not mention it)"
    system = (f"{PERSONA}\n\nWHAT YOU KNOW ABOUT NATE:\n{_format_memories(r)}\n\n"
              f"CURRENT TIME: {now}\n{weather_line}\n\n{GREETING_INSTRUCTION}")
    if voice_mode:
        system += f"\n\n{VOICE_ADDENDUM}"
    parts = []
    async for delta in chat_stream(system, [{"role": "user", "content": "(Nate just opened the session.)"}]):
        parts.append(delta)
    return "".join(parts).strip()


async def save_exchange(user_id, conversation_id, user_message, assistant_message):
    async with AsyncSessionLocal() as s:
        s.add(Message(user_id=user_id, conversation_id=conversation_id, role="user", content=user_message))
        s.add(Message(user_id=user_id, conversation_id=conversation_id, role="assistant", content=assistant_message))
        await s.commit()
    await remember(user_id, user_message, assistant_message, source_conv_id=conversation_id)

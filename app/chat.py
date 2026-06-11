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
from app.router import classify, is_machine_question as classify_is_machine
from app.tools import TOOLS, TOOL_FUNCS
from app import sysinfo
from app.memory import remember
from app.weather import get_weather
from app import computer
from app import skills
from app import music
from app.db import AsyncSessionLocal
from app.models import Message

TZ = ZoneInfo("America/Chicago")


def _format_memories(r):
    items = r["core"] + r["topic"]
    return "\n".join(f"- [{m.category}] {m.content}" for m in items) if items else "(nothing stored yet)"


VOICE_ADDENDUM = ("VOICE MODE: your reply will be spoken aloud. Flowing conversational sentences "
                  "only — never bullet points, numbered lists, markdown, or headers. "
                  "Lead with the answer and give the SHORTEST complete reply — one or two sentences "
                  "is ideal; 2-4 is the ceiling, not a target, so don't pad to fill it. Go longer "
                  "only when Nate explicitly asks to go deep.")


async def build_system_prompt(user_id, user_message, voice_mode: bool = False):
    r = await retrieve(user_id, user_message)
    now = datetime.now(TZ).strftime("%A, %B %d, %Y at %I:%M %p")
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
    """The post-classify half of a turn: 'control' local-machine action, forced-tool ack + timeout,
    or the plain tool loop. Shared by respond() and app/streaming.py so ack/timeout/terminal
    semantics exist exactly once. on_ack(text) is awaited right before a slow forced tool starts."""
    if route == "control":
        # Local-machine action. SAFE actions execute immediately; RISKY ones return a confirmation
        # request and arm the gate (resolved next turn by computer.resolve_pending, checked in the
        # callers before routing). Deterministic — the model never decides what's safe.
        print("[ROUTE: control -> local computer]", file=sys.stderr)
        return computer.handle_control(user_id, user_message)
    force = _FORCE.get(route)
    if force:
        print(f"[ROUTE: {route} -> {force}]", file=sys.stderr)
        if on_ack:
            await on_ack(_ACK[route])    # immediate feedback before the slow tool
    coro = chat_with_tools(system, window + [{"role": "user", "content": user_message}],
                           TOOLS, TOOL_FUNCS, force_tool=force, voice_mode=voice_mode)
    if force:
        try:
            # route maps 1:1 to the forced agent tool, so this applies that tool's timeout;
            # wait_for cancels chat_with_tools (and the awaited agent) cleanly on expiry
            return await asyncio.wait_for(coro, _TIMEOUTS[route])
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

    # user-defined skills run BEFORE normal routing: a saved trigger phrase runs the skill, and
    # create/list/delete are handled here too. None -> route normally.
    skill_reply = await skills.handle(user_id, user_message)
    if skill_reply is not None:
        print(skill_reply)
        log_turn("skill", "skill", time.monotonic() - t0, "rest")
        return skill_reply

    # favorite-artists management ("my favorite artists are...", add/remove/list) — deterministic,
    # works on any rung. None -> route normally.
    music_reply = music.handle(user_message)
    if music_reply is not None:
        print(music_reply)
        log_turn("music", "direct", time.monotonic() - t0, "rest")
        return music_reply

    # sysinfo DIRECT fast path: machine questions are identified deterministically and answered
    # straight from telemetry (~1s) — no retrieval, no classify, no LLM tool-round (was 9-19s).
    # None -> not confidently mappable -> normal pipeline below.
    if classify_is_machine(user_message):
        direct = await asyncio.to_thread(sysinfo.answer_machine_question, user_message)
        if direct:
            print(direct)
            log_turn("system", "direct", time.monotonic() - t0, "rest")
            return direct

    # memory retrieval and router classification are independent — overlap them so the turn
    # pays max(retrieve, classify) instead of the sum (classify alone measured 0.3-0.8s)
    system, route = await asyncio.gather(
        build_system_prompt(user_id, user_message, voice_mode=voice_mode),
        classify(user_message))

    async def _ack(text):
        print(text)                      # immediate text feedback before the slow tool
        if voice_mode and speak:
            speak(text)                  # voice: spoken immediately, before the await

    reply = await execute_route(user_id, system, route, user_message, window,
                                voice_mode=voice_mode, on_ack=_ack)
    print(reply)
    rung = "exhausted" if (reply or "").startswith(LADDER_EXHAUSTED_MSG) else current_rung.get()
    log_turn(route, rung, time.monotonic() - t0, "rest")
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

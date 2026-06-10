import re
import sys
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.persona import PERSONA
from app.retrieval import retrieve
from app.llm import chat_stream, chat_with_tools, TOOL_TIMEOUTS, TIMEOUT_MSG
from app.router import classify
from app.tools import TOOLS, TOOL_FUNCS
from app.memory import remember
from app.weather import get_weather
from app.db import AsyncSessionLocal
from app.models import Message

TZ = ZoneInfo("America/Chicago")


def _format_memories(r):
    items = r["core"] + r["topic"]
    return "\n".join(f"- [{m.category}] {m.content}" for m in items) if items else "(nothing stored yet)"


VOICE_ADDENDUM = ("VOICE MODE: your reply will be spoken aloud. Flowing conversational sentences "
                  "only — never bullet points, numbered lists, markdown, or headers. "
                  "Spoken replies are 2-4 sentences MAX unless Nate explicitly asks you to go "
                  "deep. Lead with the answer.")


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


_OPEN_SITE_RE = re.compile(
    r"(?:open|go\s+to|visit|pull\s+up|launch|bring\s+up)\s+(?:the\s+)?(?:website\s+|site\s+)?"
    r"([A-Za-z0-9][\w.-]*)", re.I)


def _open_reply(user_message: str) -> str:
    """Bare 'open <site>' — the headless server browser can't put a tab on Nate's screen, so a
    silent 2-minute browse session is the wrong move. Say so and ask for the actual task."""
    m = _OPEN_SITE_RE.search(user_message)
    site = m.group(1) if m else "that site"
    return (f"I can't open {site} on your screen — my browser runs here on the server, so you'd "
            f"never see the tab. What I can do is go read {site} and report back. What do you "
            "want from it?")


async def respond(user_id, user_message, window, voice_mode: bool = False, speak=None):
    # memory retrieval and router classification are independent — overlap them so the turn
    # pays max(retrieve, classify) instead of the sum (classify alone measured 0.3-0.8s)
    system, route = await asyncio.gather(
        build_system_prompt(user_id, user_message, voice_mode=voice_mode),
        classify(user_message))
    if route == "open":
        print("[ROUTE: open -> no agent, instant clarification]", file=sys.stderr)
        reply = _open_reply(user_message)
        print(reply)
        return reply
    force = _FORCE.get(route)
    if force:
        print(f"[ROUTE: {route} -> {force}]", file=sys.stderr)
        ack = _ACK[route]
        print(ack)                       # immediate text feedback before the slow tool
        if voice_mode and speak:
            speak(ack)                   # voice: spoken immediately, before the await
    coro = chat_with_tools(system, window + [{"role": "user", "content": user_message}],
                           TOOLS, TOOL_FUNCS, force_tool=force, voice_mode=voice_mode)
    if force:
        try:
            # route maps 1:1 to the forced agent tool, so this applies that tool's timeout;
            # wait_for cancels chat_with_tools (and the awaited agent) cleanly on expiry
            reply = await asyncio.wait_for(coro, _TIMEOUTS[route])
        except asyncio.TimeoutError:
            reply = _TIMEOUT_MSG
    else:
        reply = await coro
    print(reply)
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

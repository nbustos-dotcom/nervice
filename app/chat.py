import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from app.persona import PERSONA
from app.retrieval import retrieve
from app.llm import chat_stream, chat_with_tools
from app.router import classify
from app.tools import TOOLS, TOOL_FUNCS
from app.memory import remember
from app.db import AsyncSessionLocal
from app.models import Message

TZ = ZoneInfo("America/Chicago")


def _format_memories(r):
    items = r["core"] + r["topic"]
    return "\n".join(f"- [{m.category}] {m.content}" for m in items) if items else "(nothing stored yet)"


async def build_system_prompt(user_id, user_message):
    r = await retrieve(user_id, user_message)
    now = datetime.now(TZ).strftime("%A, %B %d, %Y at %I:%M %p")
    return f"{PERSONA}\n\nWHAT YOU KNOW ABOUT NATE:\n{_format_memories(r)}\n\nCURRENT TIME: {now}"


async def respond(user_id, user_message, window):
    system = await build_system_prompt(user_id, user_message)
    route = await classify(user_message)
    force = {"hard": "consult_claude", "build": "agent_build", "selfmod": "propose_self_update"}.get(route)
    if force:
        print(f"[ROUTE: {route} -> {force}]", file=sys.stderr)
    reply = await chat_with_tools(system, window + [{"role": "user", "content": user_message}], TOOLS, TOOL_FUNCS, force_tool=force)
    print(reply)
    return reply


async def save_exchange(user_id, conversation_id, user_message, assistant_message):
    async with AsyncSessionLocal() as s:
        s.add(Message(user_id=user_id, conversation_id=conversation_id, role="user", content=user_message))
        s.add(Message(user_id=user_id, conversation_id=conversation_id, role="assistant", content=assistant_message))
        await s.commit()
    await remember(user_id, user_message, assistant_message, source_conv_id=conversation_id)

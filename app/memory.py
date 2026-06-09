import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db import AsyncSessionLocal
from app.embeddings import embed
from app.llm import chat_json
from app.models import Memory

VALID_CATEGORIES = {"identity", "preference", "project", "relationship", "goal", "fact"}

RECONCILE_SYSTEM = """
You maintain a long-term memory store of durable facts about one user named Nate.
Given the latest conversation exchange and a list of EXISTING related memories, decide what to change.

A fact is DURABLE only if it would still be true and useful weeks from now and should change how you act in future conversations. Capture three kinds: who the user is (identity/situation), what they want (preferences, values, working style), what they're doing (projects, decisions, goals, people).
DO NOT store: task-local details, transient states or moods, the step-by-step of solving a current problem, or anything that teaches nothing lasting about the user.

For each durable fact assign:
- category: one of identity, preference, project, relationship, goal, fact
- salience 1-5: 5=core identity or hard constraints; 4=strong stable preferences or major decisions; 3=project/architecture facts; 2=minor preferences or people; 1=weak or uncertain.

Reconcile against the EXISTING memories:
- new fact duplicates an existing one -> noop (omit it)
- new fact refines/extends an existing one -> update (give its id)
- new fact contradicts an existing one -> supersede (give the old id and the corrected memory)
- otherwise -> add

Output ONLY valid JSON, no prose:
{"ops":[
  {"op":"add","content":"...","category":"...","salience":N},
  {"op":"update","id":"<id>","content":"...","salience":N},
  {"op":"supersede","id":"<id>","content":"...","category":"...","salience":N}
]}
Write each memory as a concise self-contained third-person statement ("Nate prefers ..."). If nothing is durable, return {"ops":[]}.
"""


def _validate_op(op: dict) -> bool:
    if op.get("op") not in {"add", "update", "supersede"}:
        return False
    if "content" not in op or not isinstance(op["content"], str) or not op["content"].strip():
        return False
    if op.get("op") in {"add", "supersede"}:
        if op.get("category") not in VALID_CATEGORIES:
            return False
    salience = op.get("salience")
    if not isinstance(salience, int) or not (1 <= salience <= 5):
        return False
    if op.get("op") in {"update", "supersede"} and not op.get("id"):
        return False
    return True


async def remember(
    user_id: str,
    user_text: str,
    assistant_text: str,
    source_conv_id: str | None = None,
) -> list[dict]:
    exchange = f"User: {user_text}\nAssistant: {assistant_text}"

    qvec = await embed(exchange)

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id, Memory.is_active == True)  # noqa: E712
            .order_by(Memory.embedding.cosine_distance(qvec))
            .limit(10)
        )
        related = (await session.execute(stmt)).scalars().all()

        if related:
            existing_json = [
                {"id": str(m.id), "category": m.category, "salience": m.salience, "content": m.content}
                for m in related
            ]
            import json
            existing_str = json.dumps(existing_json)
        else:
            existing_str = "none"

        user_msg = (
            f"EXISTING related memories:\n{existing_str}"
            f"\n\nLATEST exchange:\nUser: {user_text}\nAssistant: {assistant_text}"
        )

        raw = await chat_json(RECONCILE_SYSTEM, user_msg)
        ops = raw.get("ops", [])

        applied = []
        now = datetime.now(timezone.utc)

        for op in ops:
            if not _validate_op(op):
                continue

            kind = op["op"]
            content = op["content"].strip()
            salience = op["salience"]

            if kind == "add":
                vec = await embed(content)
                row = Memory(
                    user_id=user_id,
                    content=content,
                    category=op["category"],
                    salience=salience,
                    embedding=vec,
                    source_conv_id=source_conv_id,
                    source_snippet=exchange[:500],
                )
                session.add(row)
                applied.append({"op": "add", "content": content})

            elif kind == "update":
                target_id = op["id"]
                vec = await embed(content)
                await session.execute(
                    update(Memory)
                    .where(Memory.id == uuid.UUID(target_id), Memory.user_id == user_id)
                    .values(content=content, salience=salience, embedding=vec, updated_at=now)
                )
                applied.append({"op": "update", "content": content})

            elif kind == "supersede":
                old_id = op["id"]
                vec = await embed(content)
                new_row = Memory(
                    user_id=user_id,
                    content=content,
                    category=op["category"],
                    salience=salience,
                    embedding=vec,
                    source_conv_id=source_conv_id,
                    source_snippet=exchange[:500],
                )
                session.add(new_row)
                await session.flush()  # get new_row.id before updating old row

                await session.execute(
                    update(Memory)
                    .where(Memory.id == uuid.UUID(old_id), Memory.user_id == user_id)
                    .values(is_active=False, superseded_by=new_row.id, updated_at=now)
                )
                applied.append({"op": "supersede", "content": content})

        await session.commit()

    return applied

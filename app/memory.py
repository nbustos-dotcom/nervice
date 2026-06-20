import sys
import json
import uuid
import pathlib
from datetime import datetime, timezone

from sqlalchemy import select, update
from groq import RateLimitError

from app.db import AsyncSessionLocal
from app.embeddings import embed
from app.llm import chat_json
from app.models import Memory
from app import ollama_client as ollama

# When Groq is capped, extraction runs on the LOCAL rung with a salience CEILING: capped <=4 -- the
# conservative 4B (measured Q2: it tracks the 70B and never sprays highs) CAN reach the always-injected
# CORE tier (>=4), but the top 5=core-identity tier stays reserved for a 70B re-score. Every 4B op is
# logged here for batch re-verification when Groq resets. Failed extractions are queued here too -- never lost.
_REQUEUE = pathlib.Path(__file__).resolve().parent.parent / "data" / "memory_requeue.jsonl"
_LOCAL_SALIENCE_CAP = 4


def _requeue_log(entry: dict) -> None:
    try:
        _REQUEUE.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(_REQUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[memory requeue log failed] {repr(e)[:80]}", file=sys.stderr)


_SUPERSEDE_LOG = pathlib.Path(__file__).resolve().parent.parent / "data" / "supersede_log.jsonl"


def _supersede_log(old_id, new_id, new_content, salience, category, candidates, user_id, conv) -> None:
    """Append a full record of every supersede (an existing fact deactivated by a new one). Defensive
    like _requeue_log: ANY failure (incl. building the entry) is caught, logged to stderr, and
    swallowed -- it can NEVER break the supersede itself. Captures enough to REVERSE a wrong supersede
    from the log alone: old id+content, new id+content, salience, and the candidate set the model saw."""
    try:
        old_content = next((c.get("content") for c in candidates if c.get("id") == old_id),
                           "(superseded row was not in the candidate set)")
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user_id": user_id, "conv": conv,
            "old_id": old_id, "old_content": old_content,
            "new_id": new_id, "new_content": new_content,
            "salience": salience, "category": category,
            "candidates": candidates,
        }
        _SUPERSEDE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_SUPERSEDE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[supersede log failed] {repr(e)[:80]}", file=sys.stderr)


VALID_CATEGORIES = {"identity", "preference", "project", "relationship", "goal", "fact"}

RECONCILE_SYSTEM = """
You maintain a long-term memory store of durable facts about one user named Nate.
Given the latest conversation exchange and a list of EXISTING related memories, decide what to change.

A fact is DURABLE only if it would still be true and useful weeks from now and should change how you act in future conversations. Capture: who the user is (identity/situation), what they want (preferences, values, working style), and WHAT THEY'RE DOING OR PURSUING — projects, business or creative IDEAS they are developing or exploring, plans, decisions they've made, goals they've stated, and important people. An idea, plan, decision, or goal Nate describes wanting to build, try, or pursue IS durable even when it is early-stage or just an idea — capture it (e.g. "Nate is developing an automated micro-site business for local workers and businesses", "Nate decided to switch the voice to a British accent", "Nate's goal is to make the assistant respond in under two seconds"). Categorize ideas/plans/projects as "project" and stated aims as "goal".
DO NOT store: task-local details, transient states or moods, meta-observations about the conversation itself (e.g. how often Nate reopens the chat, what he is doing this session), the step-by-step of solving a current problem, or anything that teaches nothing lasting. Capture SIGNIFICANT items only — a real project, idea, decision, or goal he is actually pursuing, not every passing thought, hypothetical, or option merely weighed aloud. NEVER fabricate or infer unstated emotional/relational facts — store only what Nate actually said or what is directly and unambiguously implied; when in doubt, omit. Use the single most accurate category; a behavior or fact is not a "preference."

For each durable fact assign:
- category: one of identity, preference, project, relationship, goal, fact
- salience 1-5: 5=core identity or hard constraints; 4=strong stable preferences or major decisions; 3=project/architecture facts; 2=minor preferences or people; 1=weak or uncertain.

Reconcile against the EXISTING memories:
- new fact duplicates an existing one -> noop (omit it)
- new fact refines/extends an existing one -> update (give its id)
- new fact is an EXPLICIT change or correction to the SAME attribute of an existing fact -- Nate signals the old value changed or was wrong (cues like "now", "actually", "no longer", "not X anymore", "changed to", "instead of") -> supersede (give the old id and the corrected memory)
- otherwise -> add
A statement about a NEW, DISTINCT, hypothetical, or TEST/throwaway attribute is NOT a contradiction even when it shares words or a topic with an existing fact (e.g. "favorite TEST color is octarine" does NOT contradict "favorite color is purple") -- that is an "add", never a supersede. When in doubt whether the new statement truly REPLACES the same attribute Nate stated before, choose add or update -- NEVER supersede/destroy an existing fact on mere similarity or doubt.

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


def _reconcile_user_msg(existing_str: str, user_text: str) -> str:
    """Capture-input gate (Part 2, Option A1): the scorer sees ONLY Nate's message (plus the EXISTING
    related memories) — the assistant reply is NOT sent and is never mined. This kills the
    self-ingestion loop ("who am I" -> assistant recites memory -> it gets re-stored as new rows) and
    build/tool/proposal STATUS pollution at the source. (Measured last session: labeling the reply
    "context only" did NOT stop the 70B mining it, so it is dropped from the scored input entirely.)
    Accepted trade-off: a fact Nate states only by confirming the assistant's question ("yes") is not
    captured. The FULL exchange is still used for the related-lookup embed and source_snippet, so
    provenance stays complete. Same reconcile call, no new LLM cost; RECONCILE_SYSTEM unchanged."""
    return (
        f"EXISTING related memories:\n{existing_str}"
        f"\n\nNATE'S LATEST MESSAGE (the ONLY source for durable facts — extract from this):\n{user_text}"
    )


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
            existing_json = []
            existing_str = "none"

        user_msg = _reconcile_user_msg(existing_str, user_text)

        local_extract = False
        try:
            raw = await chat_json(RECONCILE_SYSTEM, user_msg)
        except RateLimitError:
            # Groq capped — extract on the local rung instead of silently losing the fact.
            try:
                raw = await ollama.chat_json(RECONCILE_SYSTEM, user_msg, timeout=40)
                local_extract = True
                print("[memory] groq capped -> local ollama extraction (quarantined)", file=sys.stderr)
            except Exception as e:
                # Both out: queue the exchange so nothing is lost, extract on a later pass.
                _requeue_log({"kind": "deferred", "user_id": user_id, "conv": source_conv_id,
                              "user_text": user_text[:500], "assistant_text": assistant_text[:500],
                              "reason": f"groq capped + local failed: {repr(e)[:80]}"})
                print(f"[memory] extraction deferred to requeue: {repr(e)[:80]}", file=sys.stderr)
                return []
        ops = raw.get("ops", [])

        applied = []
        now = datetime.now(timezone.utc)

        for op in ops:
            if not _validate_op(op):
                continue

            kind = op["op"]
            content = op["content"].strip()
            salience = op["salience"]
            if local_extract:
                # CEILING: a 4B extraction can now reach CORE (>=4) but never the top 5=core-identity
                # tier (reserved for a 70B re-score); flagged for re-verification when Groq resets.
                salience = min(salience, _LOCAL_SALIENCE_CAP)
                _requeue_log({"kind": "verify", "user_id": user_id, "conv": source_conv_id,
                              "op": kind, "content": content[:300], "extractor": "ollama-qwen3.5:4b"})

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
                _supersede_log(old_id=old_id, new_id=str(new_row.id), new_content=content,
                               salience=salience, category=op["category"], candidates=existing_json,
                               user_id=user_id, conv=source_conv_id)
                applied.append({"op": "supersede", "content": content})

        await session.commit()

    return applied

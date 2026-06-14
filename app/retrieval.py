import asyncio
import sys

from app.embeddings import embed
from app.db import AsyncSessionLocal
from app.models import Memory
from sqlalchemy import select

# Fix 2.2: a real relevance bar for topical recall. pgvector cosine_distance: 0 = identical,
# higher = less related. nomic-embed-text puts genuinely related content under ~0.55; unrelated
# chatter sits ~0.7+. Off-topic turns now inject NOTHING beyond the essentials.
MAX_TOPIC_DISTANCE = 0.55

# Reliability floor: each external dependency (Ollama for embeddings, Supabase for the rows) gets a
# timeout so a *flaky/hung* rung can't wedge a turn — not just a *down* one. Generous: warm recall is
# ~0.3-0.8s; a normal embed is well under 1s. On timeout/error we degrade, never block.
_RECALL_TIMEOUT_S = 8.0


async def retrieve(user_id: str, query_text: str, essentials_limit: int = 3,
                   topic_limit: int = 8) -> dict:
    """Gated two-tier memory recall (Fix 2.2). Returns {"core": [...], "topic": [...], "degraded": bool}:
      core  = ESSENTIALS: top salience-5 identity/preference facts only, max 3, always present.
      topic = everything else active (any salience), by cosine distance, ONLY above the real
              relevance threshold.

    FAIL-SOFT (reliability floor): a routed turn must NEVER 500 because the local rung or the DB
    hiccupped. The two external dependencies fail INDEPENDENTLY and SOFTLY:
      (a) embeddings (Ollama) unreachable/slow -> no query vector -> TOPIC recall is skipped, but the
          CORE essentials (which need no embedding) are still served. degraded=True.
      (b) the DB (Supabase) unreachable/slow -> CORE and TOPIC come back empty. degraded=True.
    Either way the caller (build_system_prompt) gets a valid dict and the turn answers on Groq/Ollama
    with whatever memory was reachable (possibly none). Never raises."""
    degraded = False

    # (a) embedding — Ollama. A failure here only costs the TOPIC (cosine) tier; CORE needs no vector.
    qvec = None
    try:
        qvec = await asyncio.wait_for(embed(query_text), timeout=_RECALL_TIMEOUT_S)
    except Exception as e:
        degraded = True
        print(f"[retrieval] embedding unavailable (Ollama?) — topic recall skipped this turn: "
              f"{repr(e)[:100]}", file=sys.stderr)

    # (b) the rows — Supabase. CORE is embedding-free; TOPIC runs only when we have a query vector.
    async def _query():
        async with AsyncSessionLocal() as s:
            core = (await s.execute(
                select(Memory)
                .where(Memory.user_id == user_id, Memory.is_active == True,            # noqa: E712
                       Memory.salience == 5, Memory.category.in_(("identity", "preference")))
                .order_by(Memory.updated_at.desc())
                .limit(essentials_limit)
            )).scalars().all()
            topic = []
            if qvec is not None:
                dist = Memory.embedding.cosine_distance(qvec)
                topic = (await s.execute(
                    select(Memory)
                    .where(Memory.user_id == user_id, Memory.is_active == True,         # noqa: E712
                           dist < MAX_TOPIC_DISTANCE)
                    .order_by(dist)
                    .limit(topic_limit + essentials_limit)   # headroom: essentials may appear here too
                )).scalars().all()
            return core, topic

    core, topic = [], []
    try:
        core, topic = await asyncio.wait_for(_query(), timeout=_RECALL_TIMEOUT_S)
    except Exception as e:
        degraded = True
        print(f"[retrieval] memory DB unavailable (Supabase?) — recall degraded this turn: "
              f"{repr(e)[:100]}", file=sys.stderr)

    core_ids = {m.id for m in core}
    topic = [m for m in topic if m.id not in core_ids][:topic_limit]
    return {"core": list(core), "topic": list(topic), "degraded": degraded}

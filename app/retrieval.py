from app.embeddings import embed
from app.db import AsyncSessionLocal
from app.models import Memory
from sqlalchemy import select

# Fix 2.2: a real relevance bar for topical recall. pgvector cosine_distance: 0 = identical,
# higher = less related. nomic-embed-text puts genuinely related content under ~0.55; unrelated
# chatter sits ~0.7+. Off-topic turns now inject NOTHING beyond the essentials.
MAX_TOPIC_DISTANCE = 0.55


async def retrieve(user_id: str, query_text: str, essentials_limit: int = 3,
                   topic_limit: int = 8) -> dict:
    """Gated two-tier memory recall (Fix 2.2 — replaces the old inject-all-CORE behavior that
    cost a 3k-token prefill every turn). Returns {"core": [...], "topic": [...]}:
      core  = ESSENTIALS: top salience-5 identity/preference facts only, max 3, always present.
      topic = everything else active (any salience), by cosine distance, ONLY above the real
              relevance threshold — not a fixed top-k of whatever is least-unrelated."""
    qvec = await embed(query_text)
    async with AsyncSessionLocal() as s:
        core = (await s.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.is_active == True,            # noqa: E712
                   Memory.salience == 5, Memory.category.in_(("identity", "preference")))
            .order_by(Memory.updated_at.desc())
            .limit(essentials_limit)
        )).scalars().all()
        dist = Memory.embedding.cosine_distance(qvec)
        topic = (await s.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.is_active == True,            # noqa: E712
                   dist < MAX_TOPIC_DISTANCE)
            .order_by(dist)
            .limit(topic_limit + essentials_limit)   # headroom: essentials may appear here too
        )).scalars().all()
    core_ids = {m.id for m in core}
    topic = [m for m in topic if m.id not in core_ids][:topic_limit]
    return {"core": list(core), "topic": list(topic)}

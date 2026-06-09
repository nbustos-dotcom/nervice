from app.embeddings import embed
from app.db import AsyncSessionLocal
from app.models import Memory
from sqlalchemy import select


async def retrieve(user_id: str, query_text: str, core_limit: int = 15, topic_limit: int = 8) -> dict:
    """Two-tier memory retrieval. Returns {"core": [...Memory], "topic": [...Memory]}."""
    qvec = await embed(query_text)
    async with AsyncSessionLocal() as s:
        # CORE: always-inject high-salience facts, topic-independent
        core = (await s.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.is_active == True, Memory.salience >= 4)  # noqa: E712
            .order_by(Memory.salience.desc(), Memory.updated_at.desc())
            .limit(core_limit)
        )).scalars().all()
        # TOPIC: semantic match over lower-salience memories
        topic = (await s.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.is_active == True, Memory.salience < 4)  # noqa: E712
            .order_by(Memory.embedding.cosine_distance(qvec))
            .limit(topic_limit)
        )).scalars().all()
    return {"core": list(core), "topic": list(topic)}

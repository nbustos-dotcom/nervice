import asyncio
import sys

from app.embeddings import embed
from app.db import AsyncSessionLocal
from app.models import Memory
from app import errorlog
from sqlalchemy import select

# Fix 2.2: a real relevance bar for topical recall. pgvector cosine_distance: 0 = identical,
# higher = less related. nomic-embed-text puts genuinely related content under ~0.55; unrelated
# chatter sits ~0.7+. Off-topic turns now inject NOTHING beyond the essentials.
MAX_TOPIC_DISTANCE = 0.55      # cosine-distance gate for the TOPIC tier — UNCHANGED (measured: raising
                               # it drags the gate into the 0.7+ noise band; the fix is CORE breadth)

# Part 1 (CORE breadth, 2026-06-19): the always-injected ESSENTIALS tier. Was salience == 5 AND
# category in (identity, preference) — only 1 of 86 stored facts qualified, so generic "who am I"
# turns (which don't topically hook any single fact — distance 0.58-0.72) recalled nothing useful.
# Now ANY category at salience >= CORE_MIN_SALIENCE, ordered salience-first, capped at CORE_LIMIT.
CORE_MIN_SALIENCE = 4          # essentials floor (was a hardcoded == 5)
CORE_LIMIT = 10                # max essentials injected — >= the whole >=4 set so the cap never
                               # silently truncates on updated_at ordering (recall must not hinge on
                               # which fact was edited most recently)
TOPIC_LIMIT = 8                # max topical (cosine) matches injected

# Reliability floor: each external dependency (Ollama for embeddings, Supabase for the rows) gets a
# timeout so a *flaky/hung* rung can't wedge a turn — not just a *down* one. Generous: warm recall is
# ~0.3-0.8s; a normal embed is well under 1s. On timeout/error we degrade, never block.
_RECALL_TIMEOUT_S = 8.0


async def retrieve(user_id: str, query_text: str, essentials_limit: int = CORE_LIMIT,
                   topic_limit: int = TOPIC_LIMIT) -> dict:
    """Gated two-tier memory recall (Fix 2.2; CORE breadth Part 1). Returns
    {"core": [...], "topic": [...], "degraded": bool}:
      core  = ESSENTIALS: any-category facts at salience >= CORE_MIN_SALIENCE (4), highest salience
              first, max CORE_LIMIT, ALWAYS present (no embedding needed).
      topic = everything else active (any salience), by cosine distance, ONLY under MAX_TOPIC_DISTANCE.

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
        errorlog.log_error("recall:embed", e, query_text)   # central capture; turn still fail-soft below

    # (b) the rows — Supabase. CORE is embedding-free; TOPIC runs only when we have a query vector.
    async def _query():
        async with AsyncSessionLocal() as s:
            core = (await s.execute(
                select(Memory)
                .where(Memory.user_id == user_id, Memory.is_active == True,            # noqa: E712
                       Memory.salience >= CORE_MIN_SALIENCE)
                .order_by(Memory.salience.desc(), Memory.updated_at.desc())
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
        errorlog.log_error("recall:db", e, query_text)      # central capture; CORE still served below

    core_ids = {m.id for m in core}
    topic = [m for m in topic if m.id not in core_ids][:topic_limit]
    return {"core": list(core), "topic": list(topic), "degraded": degraded}

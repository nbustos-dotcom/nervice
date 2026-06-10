import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import Base  # noqa: F401

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# pool_pre_ping: a checkout on a connection the Supabase pooler silently killed reconnects
# transparently (~50ms ping) instead of erroring or stalling the turn. Deliberately NO
# pool_recycle — it's age-based, so alongside the API's keepalive it would just force a fresh
# ~2s reconnect every interval. The keepalive (app/api.py) is what keeps the pool warm.
engine = create_async_engine(DATABASE_URL, connect_args={"statement_cache_size": 0},
                             pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

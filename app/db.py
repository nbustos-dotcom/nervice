import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models import Base  # noqa: F401

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_async_engine(DATABASE_URL, connect_args={"statement_cache_size": 0})
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

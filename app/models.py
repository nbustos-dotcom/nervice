from sqlalchemy import Boolean, ForeignKey, Integer, SmallInteger, Text, Uuid
from sqlalchemy import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, mapped_column, Mapped
from sqlalchemy.sql import text
from pgvector.sqlalchemy import Vector
import uuid


class Base(DeclarativeBase):
    pass


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    salience: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    embedding = mapped_column(Vector(768), nullable=True)
    source_conv_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("memories.id"), nullable=True)
    created_at = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    updated_at = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))
    last_accessed_at = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at = mapped_column(TIMESTAMP(timezone=True), server_default=text("now()"))

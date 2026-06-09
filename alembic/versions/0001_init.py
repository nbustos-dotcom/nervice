"""init

Revision ID: 0001
Revises:
Create Date: 2026-06-08

"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("create extension if not exists pgcrypto")
    op.execute("create extension if not exists vector")
    op.execute("""
        create table memories (
            id uuid primary key default gen_random_uuid(),
            user_id text not null,
            content text not null,
            category text not null check (category in ('identity','preference','project','relationship','goal','fact')),
            salience smallint not null check (salience between 1 and 5),
            embedding vector(768),
            source_conv_id text,
            source_snippet text,
            is_active boolean not null default true,
            superseded_by uuid references memories(id),
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now(),
            last_accessed_at timestamptz,
            access_count int not null default 0
        )
    """)
    op.execute("create index ix_memories_user_active on memories (user_id, is_active)")
    op.execute("create index ix_memories_category on memories (category)")
    op.execute("create index ix_memories_embedding on memories using hnsw (embedding vector_cosine_ops)")
    op.execute("""
        create table messages (
            id uuid primary key default gen_random_uuid(),
            user_id text not null,
            conversation_id text not null,
            role text not null,
            content text not null,
            created_at timestamptz not null default now()
        )
    """)
    op.execute("create index ix_messages_conversation on messages (conversation_id, created_at)")


def downgrade() -> None:
    op.execute("drop index if exists ix_messages_conversation")
    op.execute("drop table if exists messages")
    op.execute("drop index if exists ix_memories_embedding")
    op.execute("drop index if exists ix_memories_category")
    op.execute("drop index if exists ix_memories_user_active")
    op.execute("drop table if exists memories")

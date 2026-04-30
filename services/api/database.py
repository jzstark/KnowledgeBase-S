import json
import os

import databases

DATABASE_URL = os.environ["DATABASE_URL"]

database = databases.Database(DATABASE_URL)


def jsonb(value: dict) -> str:
    """将 dict 序列化为 JSON 字符串，供 asyncpg JSONB 参数使用。"""
    return json.dumps(value, ensure_ascii=False)

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    title TEXT,
    abstract TEXT,
    embedding vector(1536),
    source_type VARCHAR,
    source_id VARCHAR,
    raw_ref JSONB,
    tags TEXT[],
    is_primary BOOLEAN DEFAULT true,
    object_type VARCHAR(16) NOT NULL DEFAULT 'article',
    source_node_ids TEXT[] DEFAULT '{}',
    summary_of VARCHAR,
    canonical_name TEXT,
    aliases TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entity_candidates (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    embedding vector(1536),
    mentions JSONB DEFAULT '[]',
    promoted_entity_id VARCHAR REFERENCES knowledge_nodes(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, canonical_name)
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    id SERIAL PRIMARY KEY,
    from_node_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    to_node_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    relation_type VARCHAR,
    weight FLOAT,
    created_by VARCHAR
);

CREATE TABLE IF NOT EXISTS writing_memory (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    template_name VARCHAR,
    rule TEXT,
    rule_type VARCHAR,
    confidence FLOAT DEFAULT 0.5,
    count INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sources (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    type VARCHAR NOT NULL,
    fetch_mode VARCHAR,
    is_primary BOOLEAN DEFAULT true,
    config JSONB,
    api_token VARCHAR,
    last_fetched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS drafts (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    template_name VARCHAR,
    selected_node_ids TEXT[],
    draft_content TEXT,
    final_content TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS briefings (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    groups JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS topics (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    title TEXT NOT NULL,
    description TEXT,
    source_node_ids TEXT[] DEFAULT '{}',
    status VARCHAR DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id VARCHAR PRIMARY KEY,
    settings JSONB NOT NULL DEFAULT '{}'
);

ALTER TABLE IF EXISTS drafts ADD COLUMN IF NOT EXISTS selected_topic_ids TEXT[];
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS object_type VARCHAR(16) NOT NULL DEFAULT 'article';
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_node_ids TEXT[] DEFAULT '{}';
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS summary_of VARCHAR;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS canonical_name TEXT;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS aliases TEXT[] DEFAULT '{}';
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS abstract TEXT;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS perspective TEXT;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS priority_score FLOAT DEFAULT 1.0;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS access_count INT DEFAULT 0;
ALTER TABLE IF EXISTS knowledge_edges ADD COLUMN IF NOT EXISTS description TEXT;

CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_user_id ON knowledge_nodes(user_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_embedding ON knowledge_nodes
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_object_type ON knowledge_nodes(object_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_summary_of ON knowledge_nodes(summary_of);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_priority ON knowledge_nodes(priority_score);
CREATE INDEX IF NOT EXISTS idx_entity_candidates_user ON entity_candidates(user_id);
CREATE INDEX IF NOT EXISTS idx_sources_user_id ON sources(user_id);
CREATE INDEX IF NOT EXISTS idx_drafts_user_id ON drafts(user_id);
CREATE INDEX IF NOT EXISTS idx_briefings_user_date ON briefings(user_id, date);
CREATE INDEX IF NOT EXISTS idx_topics_user_date ON topics(user_id, date);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    title TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, created_at);
"""


async def init():
    await database.connect()
    # 分语句执行，跳过空语句
    for stmt in SCHEMA_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            await database.execute(stmt)

    # Migration: rename summary → abstract (idempotent)
    has_summary = await database.fetch_one(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='knowledge_nodes' AND column_name='summary'"
    )
    if has_summary:
        has_abstract = await database.fetch_one(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='knowledge_nodes' AND column_name='abstract'"
        )
        if has_abstract:
            # Both columns exist: drop the old one (abstract was already added by ADD COLUMN)
            await database.execute("ALTER TABLE knowledge_nodes DROP COLUMN summary")
        else:
            await database.execute("ALTER TABLE knowledge_nodes RENAME COLUMN summary TO abstract")

    # Add FK constraint for summary_of self-reference (idempotent)
    row = await database.fetch_one(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='fk_summary_of' AND table_name='knowledge_nodes'"
    )
    if not row:
        await database.execute(
            "ALTER TABLE knowledge_nodes ADD CONSTRAINT fk_summary_of "
            "FOREIGN KEY (summary_of) REFERENCES knowledge_nodes(id) ON DELETE CASCADE"
        )

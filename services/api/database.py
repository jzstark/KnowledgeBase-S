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
    summary TEXT,
    embedding vector(1536),
    source_type VARCHAR,
    source_id VARCHAR,
    raw_ref JSONB,
    tags TEXT[],
    is_primary BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW()
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

CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_user_id ON knowledge_nodes(user_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_embedding ON knowledge_nodes
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_sources_user_id ON sources(user_id);
CREATE INDEX IF NOT EXISTS idx_drafts_user_id ON drafts(user_id);
"""


async def init():
    await database.connect()
    # 分语句执行，跳过空语句
    for stmt in SCHEMA_SQL.split(";"):
        stmt = stmt.strip()
        if stmt:
            await database.execute(stmt)

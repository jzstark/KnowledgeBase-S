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
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    source_published_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    perspective_label TEXT,
    perspective_instruction TEXT,
    perspective_embedding vector(1536),
    body_embedding vector(1536),
    is_default BOOLEAN DEFAULT false,
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

CREATE TABLE IF NOT EXISTS source_items (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    source_id VARCHAR REFERENCES sources(id) ON DELETE CASCADE,
    source_type VARCHAR NOT NULL,
    origin_ref TEXT NOT NULL,
    origin_ref_type VARCHAR NOT NULL,
    raw_snapshot_ref TEXT,
    extracted_text_ref TEXT,
    content_hash VARCHAR,
    title TEXT,
    source_published_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    raw_retention_policy VARCHAR DEFAULT 'keep_extracted_only',
    status VARCHAR NOT NULL DEFAULT 'pending',
    error TEXT,
    attempts INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, source_id, origin_ref_type, origin_ref)
);

CREATE TABLE IF NOT EXISTS article_nodes (
    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    source_item_id VARCHAR,
    raw_ref JSONB,
    source_type VARCHAR,
    source_published_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    tags TEXT[] DEFAULT '{}',
    status VARCHAR DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS summary_nodes (
    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    summary_of VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    perspective_label TEXT,
    perspective_instruction TEXT,
    perspective_embedding vector(1536),
    body TEXT,
    body_embedding vector(1536),
    is_default BOOLEAN DEFAULT false,
    source JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entity_nodes (
    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    canonical_name TEXT,
    aliases TEXT[] DEFAULT '{}',
    entity_type VARCHAR,
    merged_into VARCHAR REFERENCES knowledge_nodes(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS index_nodes (
    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    description TEXT,
    rollup_instruction TEXT,
    abstract_stale BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS index_children (
    index_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    child_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    position INT DEFAULT 0,
    child_role VARCHAR DEFAULT 'member',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (index_id, child_id)
);

CREATE TABLE IF NOT EXISTS entity_facts (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    entity_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    article_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    source_item_id VARCHAR,
    fact_text TEXT NOT NULL,
    fact_time TIMESTAMPTZ,
    source_published_at TIMESTAMPTZ,
    evidence_span TEXT,
    confidence FLOAT DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (entity_id, article_id, fact_text)
);

CREATE TABLE IF NOT EXISTS entity_profiles (
    entity_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    profile_text TEXT,
    timeline_summary TEXT,
    status VARCHAR DEFAULT 'stale',
    facts_count INT DEFAULT 0,
    refreshed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entity_pair_signals (
    entity_a_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    entity_b_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    co_occurrence_count INT DEFAULT 0,
    co_occurrence_score FLOAT DEFAULT 0,
    embedding_similarity FLOAT DEFAULT 0,
    graph_proximity_score FLOAT DEFAULT 0,
    temporal_score FLOAT DEFAULT 0,
    relatedness_score FLOAT DEFAULT 0,
    explanation TEXT,
    source_article_ids TEXT[] DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (entity_a_id, entity_b_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    job_type VARCHAR NOT NULL,
    provider VARCHAR,
    model VARCHAR,
    payload JSONB NOT NULL DEFAULT '{}',
    status VARCHAR NOT NULL DEFAULT 'pending',
    priority INT DEFAULT 0,
    idempotency_key VARCHAR,
    attempts INT DEFAULT 0,
    max_attempts INT DEFAULT 3,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
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
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS effective_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS perspective_label TEXT;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS perspective_instruction TEXT;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS perspective_embedding vector(1536);
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS body_embedding vector(1536);
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT false;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_item_id VARCHAR;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS embedding_model VARCHAR;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS doc_kind VARCHAR;
ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS default_doc_kind VARCHAR;
ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS doc_kind VARCHAR;
ALTER TABLE IF EXISTS knowledge_edges ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS source_type VARCHAR;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS origin_ref TEXT;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS origin_ref_type VARCHAR;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS raw_snapshot_ref TEXT;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS extracted_text_ref TEXT;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS content_hash VARCHAR;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS effective_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS raw_retention_policy VARCHAR DEFAULT 'keep_extracted_only';
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'pending';
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS attempts INT DEFAULT 0;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS source_item_id VARCHAR;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS raw_ref JSONB;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS source_type VARCHAR;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS effective_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'active';
ALTER TABLE IF EXISTS article_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS summary_of VARCHAR;
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS perspective_label TEXT;
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS perspective_instruction TEXT;
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS perspective_embedding vector(1536);
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS body_embedding vector(1536);
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT false;
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS source JSONB DEFAULT '{}';
ALTER TABLE IF EXISTS summary_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS entity_nodes ADD COLUMN IF NOT EXISTS canonical_name TEXT;
ALTER TABLE IF EXISTS entity_nodes ADD COLUMN IF NOT EXISTS aliases TEXT[] DEFAULT '{}';
ALTER TABLE IF EXISTS entity_nodes ADD COLUMN IF NOT EXISTS entity_type VARCHAR;
ALTER TABLE IF EXISTS entity_nodes ADD COLUMN IF NOT EXISTS merged_into VARCHAR;
ALTER TABLE IF EXISTS entity_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS index_nodes ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE IF EXISTS index_nodes ADD COLUMN IF NOT EXISTS rollup_instruction TEXT;
ALTER TABLE IF EXISTS index_nodes ADD COLUMN IF NOT EXISTS abstract_stale BOOLEAN DEFAULT false;
ALTER TABLE IF EXISTS index_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS index_children ADD COLUMN IF NOT EXISTS position INT DEFAULT 0;
ALTER TABLE IF EXISTS index_children ADD COLUMN IF NOT EXISTS child_role VARCHAR DEFAULT 'member';
ALTER TABLE IF EXISTS index_children ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS user_id VARCHAR;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS entity_id VARCHAR;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS article_id VARCHAR;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS source_item_id VARCHAR;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS fact_text TEXT;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS fact_time TIMESTAMPTZ;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS evidence_span TEXT;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS confidence FLOAT DEFAULT 0.5;
ALTER TABLE IF EXISTS entity_facts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS entity_profiles ADD COLUMN IF NOT EXISTS profile_text TEXT;
ALTER TABLE IF EXISTS entity_profiles ADD COLUMN IF NOT EXISTS timeline_summary TEXT;
ALTER TABLE IF EXISTS entity_profiles ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'stale';
ALTER TABLE IF EXISTS entity_profiles ADD COLUMN IF NOT EXISTS facts_count INT DEFAULT 0;
ALTER TABLE IF EXISTS entity_profiles ADD COLUMN IF NOT EXISTS refreshed_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS entity_profiles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS co_occurrence_count INT DEFAULT 0;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS co_occurrence_score FLOAT DEFAULT 0;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS embedding_similarity FLOAT DEFAULT 0;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS graph_proximity_score FLOAT DEFAULT 0;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS temporal_score FLOAT DEFAULT 0;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS relatedness_score FLOAT DEFAULT 0;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS explanation TEXT;
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS source_article_ids TEXT[] DEFAULT '{}';
ALTER TABLE IF EXISTS entity_pair_signals ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS user_id VARCHAR;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS job_type VARCHAR;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS provider VARCHAR;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS model VARCHAR;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}';
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'pending';
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS priority INT DEFAULT 0;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS attempts INT DEFAULT 0;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS max_attempts INT DEFAULT 3;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS result JSONB;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

UPDATE knowledge_nodes
SET ingested_at = COALESCE(ingested_at, created_at, NOW()),
    captured_at = COALESCE(captured_at, created_at, ingested_at, NOW())
WHERE ingested_at IS NULL OR captured_at IS NULL;

UPDATE knowledge_nodes
SET perspective_label = COALESCE(NULLIF(perspective_label, ''), NULLIF(perspective, ''), 'default'),
    perspective_instruction = COALESCE(NULLIF(perspective_instruction, ''), NULLIF(perspective, ''), '默认摘要'),
    is_default = COALESCE(is_default, false) OR perspective IS NULL OR perspective = '' OR perspective = 'default',
    body_embedding = COALESCE(body_embedding, embedding),
    perspective_embedding = COALESCE(perspective_embedding, body_embedding, embedding)
WHERE object_type = 'summary';

INSERT INTO article_nodes
  (node_id, source_item_id, raw_ref, source_type, source_published_at,
   source_updated_at, captured_at, effective_at, tags, status, created_at, updated_at)
SELECT id, source_item_id, raw_ref, source_type, source_published_at,
       source_updated_at, captured_at, effective_at, COALESCE(tags, '{}'),
       'active', created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'article'
ON CONFLICT (node_id) DO UPDATE SET
  source_item_id = EXCLUDED.source_item_id,
  raw_ref = EXCLUDED.raw_ref,
  source_type = EXCLUDED.source_type,
  source_published_at = EXCLUDED.source_published_at,
  source_updated_at = EXCLUDED.source_updated_at,
  captured_at = EXCLUDED.captured_at,
  effective_at = EXCLUDED.effective_at,
  tags = EXCLUDED.tags,
  updated_at = NOW();

INSERT INTO summary_nodes
  (node_id, summary_of, perspective_label, perspective_instruction,
   perspective_embedding, body, body_embedding, is_default, source, created_at, updated_at)
SELECT id, summary_of, perspective_label, perspective_instruction,
       perspective_embedding, abstract, COALESCE(body_embedding, embedding),
       COALESCE(is_default, false),
       jsonb_build_object('source_node_ids', COALESCE(source_node_ids, '{}'), 'legacy_perspective', perspective),
       created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'summary'
ON CONFLICT (node_id) DO UPDATE SET
  summary_of = EXCLUDED.summary_of,
  perspective_label = EXCLUDED.perspective_label,
  perspective_instruction = EXCLUDED.perspective_instruction,
  perspective_embedding = EXCLUDED.perspective_embedding,
  body = EXCLUDED.body,
  body_embedding = EXCLUDED.body_embedding,
  is_default = EXCLUDED.is_default,
  source = EXCLUDED.source,
  updated_at = NOW();

INSERT INTO entity_nodes
  (node_id, canonical_name, aliases, created_at, updated_at)
SELECT id, canonical_name, COALESCE(aliases, '{}'), created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'entity'
ON CONFLICT (node_id) DO UPDATE SET
  canonical_name = EXCLUDED.canonical_name,
  aliases = EXCLUDED.aliases,
  updated_at = NOW();

INSERT INTO index_nodes
  (node_id, description, abstract_stale, created_at, updated_at)
SELECT id, abstract, false, created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'index'
ON CONFLICT (node_id) DO UPDATE SET
  description = EXCLUDED.description,
  updated_at = NOW();

INSERT INTO index_children
  (index_id, child_id, position, child_role, created_at, updated_at)
SELECT ke.to_node_id,
       ke.from_node_id,
       ROW_NUMBER() OVER (PARTITION BY ke.to_node_id ORDER BY ke.id) - 1,
       'member',
       NOW(),
       NOW()
FROM knowledge_edges ke
JOIN knowledge_nodes parent ON parent.id = ke.to_node_id AND parent.object_type = 'index'
JOIN knowledge_nodes child ON child.id = ke.from_node_id AND child.object_type IN ('article', 'index')
WHERE ke.relation_type = 'part_of'
ON CONFLICT (index_id, child_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_user_id ON knowledge_nodes(user_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_embedding ON knowledge_nodes
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_object_type ON knowledge_nodes(object_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_summary_of ON knowledge_nodes(summary_of);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_priority ON knowledge_nodes(priority_score);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_knowledge_time ON knowledge_nodes(
    COALESCE(effective_at, source_published_at, captured_at, ingested_at)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_body_embedding ON knowledge_nodes
    USING ivfflat (body_embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_perspective_embedding ON knowledge_nodes
    USING ivfflat (perspective_embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_entity_candidates_user ON entity_candidates(user_id);
CREATE INDEX IF NOT EXISTS idx_sources_user_id ON sources(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_items_user_source_origin
    ON source_items(user_id, source_id, origin_ref_type, origin_ref);
CREATE INDEX IF NOT EXISTS idx_source_items_source_status ON source_items(source_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_source_items_user_status ON source_items(user_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_source_item_id ON knowledge_nodes(source_item_id);
CREATE INDEX IF NOT EXISTS idx_article_nodes_source_item_id ON article_nodes(source_item_id);
CREATE INDEX IF NOT EXISTS idx_article_nodes_knowledge_time ON article_nodes(
    COALESCE(effective_at, source_published_at, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_summary_nodes_summary_of ON summary_nodes(summary_of);
CREATE INDEX IF NOT EXISTS idx_summary_nodes_body_embedding ON summary_nodes
    USING ivfflat (body_embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_summary_nodes_perspective_embedding ON summary_nodes
    USING ivfflat (perspective_embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_entity_nodes_canonical_name ON entity_nodes(canonical_name);
CREATE INDEX IF NOT EXISTS idx_entity_nodes_merged_into ON entity_nodes(merged_into);
CREATE INDEX IF NOT EXISTS idx_index_children_index_position ON index_children(index_id, position);
CREATE INDEX IF NOT EXISTS idx_index_children_child_id ON index_children(child_id);
CREATE INDEX IF NOT EXISTS idx_entity_facts_entity_time ON entity_facts(entity_id, fact_time DESC);
CREATE INDEX IF NOT EXISTS idx_entity_facts_article ON entity_facts(article_id);
CREATE INDEX IF NOT EXISTS idx_entity_facts_source_item ON entity_facts(source_item_id);
CREATE INDEX IF NOT EXISTS idx_entity_profiles_status ON entity_profiles(status);
CREATE INDEX IF NOT EXISTS idx_entity_pair_signals_a_score ON entity_pair_signals(entity_a_id, relatedness_score DESC);
CREATE INDEX IF NOT EXISTS idx_entity_pair_signals_b_score ON entity_pair_signals(entity_b_id, relatedness_score DESC);
DROP INDEX IF EXISTS uq_jobs_user_idempotency_key;
CREATE INDEX IF NOT EXISTS idx_jobs_user_status_created ON jobs(user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_jobs_user_idempotency_key
    ON jobs(user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_drafts_user_id ON drafts(user_id);
CREATE INDEX IF NOT EXISTS idx_briefings_user_date ON briefings(user_id, date);
CREATE INDEX IF NOT EXISTS idx_topics_user_date ON topics(user_id, date);

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

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
    object_type VARCHAR(16) NOT NULL DEFAULT 'article',
    source_node_ids TEXT[] DEFAULT '{}',
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    source_published_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS entity_candidates (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    embedding vector(1536),
    mention_count INT DEFAULT 0,
    max_salience FLOAT DEFAULT 0,
    source_article_ids TEXT[] DEFAULT '{}',
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
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS abstract TEXT;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS effective_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS source_item_id VARCHAR;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS embedding_model VARCHAR;
ALTER TABLE IF EXISTS knowledge_nodes ADD COLUMN IF NOT EXISTS doc_kind VARCHAR;
ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS default_doc_kind VARCHAR;
ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS source_items ADD COLUMN IF NOT EXISTS doc_kind VARCHAR;
ALTER TABLE IF EXISTS entity_candidates ADD COLUMN IF NOT EXISTS mention_count INT DEFAULT 0;
ALTER TABLE IF EXISTS entity_candidates ADD COLUMN IF NOT EXISTS max_salience FLOAT DEFAULT 0;
ALTER TABLE IF EXISTS entity_candidates ADD COLUMN IF NOT EXISTS source_article_ids TEXT[] DEFAULT '{}';
ALTER TABLE IF EXISTS knowledge_edges ADD COLUMN IF NOT EXISTS description TEXT;

-- Phase A 第三批：删除应用层遗留字段（无任何代码引用）
DROP INDEX IF EXISTS idx_knowledge_nodes_priority;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS priority_score;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS last_accessed_at;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS access_count;

-- 延后项 1：node 级 is_primary 删除（保留 sources.is_primary）
-- briefing 改为 JOIN sources.is_primary 过滤
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS is_primary;

-- 延后项 2：entity_profiles 表删除（entity 描述统一回到 nodes.abstract）
DROP TABLE IF EXISTS entity_profiles CASCADE;

-- 延后项 3：knowledge_nodes 对象专属字段裁剪
-- summary 专属：perspective_* / body_embedding / is_default / summary_of / perspective (legacy alias)
-- entity 专属：canonical_name / aliases
-- 这些字段的权威值已在 object tables（summary_nodes / entity_nodes），DROP 后 fetch_node_with_object_fields 仍正常工作
DROP INDEX IF EXISTS idx_knowledge_nodes_summary_of;
DROP INDEX IF EXISTS idx_knowledge_nodes_body_embedding;
DROP INDEX IF EXISTS idx_knowledge_nodes_perspective_embedding;
ALTER TABLE IF EXISTS knowledge_nodes DROP CONSTRAINT IF EXISTS fk_summary_of;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS summary_of;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS canonical_name;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS aliases;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS perspective;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS perspective_label;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS perspective_instruction;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS perspective_embedding;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS body_embedding;
ALTER TABLE IF EXISTS knowledge_nodes DROP COLUMN IF EXISTS is_default;

-- Phase A 第三批：删除所有 summarizes 边（由 summary_nodes.summary_of FK 替代）
-- 幂等：再次执行 DELETE 影响零行
DELETE FROM knowledge_edges WHERE relation_type = 'summarizes';

-- Phase A 第三批：knowledge_edges 去重（保留每组最小 id）
-- 幂等：去重后再次执行影响零行
DELETE FROM knowledge_edges WHERE id IN (
    SELECT id FROM (
        SELECT id, row_number() OVER (
            PARTITION BY from_node_id, to_node_id, relation_type ORDER BY id
        ) AS rn
        FROM knowledge_edges
    ) t WHERE rn > 1
);
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

-- Phase 0 backfill INSERT...SELECT into object tables 已经执行过；
-- 延后项 3 之后，knowledge_nodes 上的对象专属字段（summary_of / canonical_name /
-- aliases / perspective_* / body_embedding / is_default）全部被 DROP，再次执行此类
-- backfill 会因列缺失失败。object_nodes 模块的 upsert 已是这些字段的权威写入路径。

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
-- idx_knowledge_nodes_summary_of 已删除（列同时随延后项 3 DROP）；
-- summary_nodes 自带 idx_summary_nodes_summary_of 索引覆盖该路径
CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_knowledge_time ON knowledge_nodes(
    COALESCE(effective_at, source_published_at, captured_at, ingested_at)
);
-- idx_knowledge_nodes_{body,perspective}_embedding 已删除（列同时随延后项 3 DROP）；
-- summary_nodes 自带 idx_summary_nodes_body_embedding / idx_summary_nodes_perspective_embedding 覆盖该路径
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

    # （延后项 3 之前曾在此添加 fk_summary_of FK；summary_of 列已从 knowledge_nodes
    # 删除，约束随 DROP COLUMN 一并消失，无需再添加）

    # Phase A 第三批：entity_candidates.mentions JSONB → source_article_ids TEXT[]
    # 条件迁移：先 backfill 再 DROP（DO 块在 SCHEMA_SQL split-by-; 的拆分中无法直接用）
    has_mentions = await database.fetch_one(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='entity_candidates' AND column_name='mentions'"
    )
    if has_mentions:
        # 一次性把 JSONB mentions 浓缩为 source_article_ids + 计数器（在 DROP 之前）
        await database.execute(
            """
            UPDATE entity_candidates
            SET source_article_ids = ARRAY(
                SELECT DISTINCT (elem->>'article_id')
                FROM jsonb_array_elements(COALESCE(mentions, '[]'::jsonb)) AS elem
                WHERE elem->>'article_id' IS NOT NULL
            )
            WHERE (source_article_ids IS NULL OR cardinality(source_article_ids) = 0)
              AND jsonb_array_length(COALESCE(mentions, '[]'::jsonb)) > 0
            """
        )
        await database.execute(
            """
            UPDATE entity_candidates
            SET mention_count = jsonb_array_length(COALESCE(mentions, '[]'::jsonb)),
                max_salience = COALESCE(
                    (SELECT MAX((elem->>'salience')::float)
                     FROM jsonb_array_elements(COALESCE(mentions, '[]'::jsonb)) AS elem),
                    0
                )
            WHERE COALESCE(mention_count, 0) = 0
              AND jsonb_array_length(COALESCE(mentions, '[]'::jsonb)) > 0
            """
        )
        await database.execute("ALTER TABLE entity_candidates DROP COLUMN mentions")

    # 兜底：若历史迁移漏掉计数器（mention_count=0 但 source_article_ids 已有内容），
    # 用数组长度补 mention_count；max_salience 丢失，给一个保守默认 0.5
    await database.execute(
        """
        UPDATE entity_candidates
        SET mention_count = cardinality(source_article_ids),
            max_salience = GREATEST(COALESCE(max_salience, 0), 0.5)
        WHERE COALESCE(mention_count, 0) = 0
          AND source_article_ids IS NOT NULL
          AND cardinality(source_article_ids) > 0
        """
    )

    # Phase A 第三批：knowledge_edges 唯一约束 UNIQUE(from_node_id, to_node_id, relation_type)
    # 去重 DELETE 已在 SCHEMA_SQL 中执行，此处仅添加约束（幂等）
    row = await database.fetch_one(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name='uq_edges_from_to_type' AND table_name='knowledge_edges'"
    )
    if not row:
        await database.execute(
            "ALTER TABLE knowledge_edges ADD CONSTRAINT uq_edges_from_to_type "
            "UNIQUE (from_node_id, to_node_id, relation_type)"
        )

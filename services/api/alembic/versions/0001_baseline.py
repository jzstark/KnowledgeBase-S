"""baseline schema (Phase A/B final state)

Adopted from the former database.py SCHEMA_SQL. Idempotent (every statement is
IF NOT EXISTS / guarded), so `alembic upgrade head` against the existing
production database is a no-op that simply records this revision — no manual
`alembic stamp` is required. A fresh database is built from scratch here.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-10
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


# Final-form DDL. Generated from the historical SCHEMA_SQL with all one-time
# data backfills and legacy ALTER/DROP/rename steps removed; the three columns
# that existed only via ALTER (source_items.doc_kind, {article_nodes,
# source_items}.document_instance_id) are folded back in below.
STATEMENTS = [
    'CREATE EXTENSION IF NOT EXISTS vector',
    "CREATE TABLE IF NOT EXISTS knowledge_nodes (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    title TEXT,\n    abstract TEXT,\n    embedding vector(1536),\n    embedding_model VARCHAR,\n    source_id VARCHAR,\n    tags TEXT[],\n    doc_kind VARCHAR,\n    object_type VARCHAR(16) NOT NULL DEFAULT 'article',\n    ingested_at TIMESTAMPTZ DEFAULT NOW(),\n    published_at TIMESTAMPTZ,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    "CREATE TABLE IF NOT EXISTS entity_candidates (\n    id SERIAL PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    canonical_name TEXT NOT NULL,\n    aliases TEXT[] DEFAULT '{}',\n    embedding vector(1536),\n    mention_count INT DEFAULT 0,\n    max_salience FLOAT DEFAULT 0,\n    source_article_ids TEXT[] DEFAULT '{}',\n    promoted_entity_id VARCHAR REFERENCES knowledge_nodes(id),\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW(),\n    UNIQUE (user_id, canonical_name)\n)",
    "CREATE TABLE IF NOT EXISTS knowledge_edges (\n    id SERIAL PRIMARY KEY,\n    from_node_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    to_node_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    relation_type VARCHAR,\n    weight FLOAT,\n    metadata JSONB DEFAULT '{}',\n    created_by VARCHAR\n)",
    'CREATE TABLE IF NOT EXISTS sources (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    name VARCHAR NOT NULL,\n    type VARCHAR NOT NULL,\n    fetch_mode VARCHAR,\n    is_primary BOOLEAN DEFAULT true,\n    default_doc_kind VARCHAR,\n    deleted_at TIMESTAMPTZ,\n    config JSONB,\n    api_token VARCHAR,\n    last_fetched_at TIMESTAMPTZ,\n    created_at TIMESTAMPTZ DEFAULT NOW()\n)',
    "CREATE TABLE IF NOT EXISTS source_items (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    source_id VARCHAR REFERENCES sources(id) ON DELETE CASCADE,\n    source_type VARCHAR NOT NULL,\n    origin_ref TEXT NOT NULL,\n    origin_ref_type VARCHAR NOT NULL,\n    raw_snapshot_ref TEXT,\n    extracted_text_ref TEXT,\n    content_hash VARCHAR,\n    title TEXT,\n    source_published_at TIMESTAMPTZ,\n    source_updated_at TIMESTAMPTZ,\n    captured_at TIMESTAMPTZ,\n    effective_at TIMESTAMPTZ,\n    raw_retention_policy VARCHAR DEFAULT 'keep_extracted_only',\n    status VARCHAR NOT NULL DEFAULT 'pending',\n    error TEXT,\n    attempts INT DEFAULT 0,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW(),\n    UNIQUE (user_id, source_id, origin_ref_type, origin_ref)\n)",
    "CREATE TABLE IF NOT EXISTS article_nodes (\n    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    source_item_id VARCHAR,\n    raw_ref JSONB,\n    source_type VARCHAR,\n    source_published_at TIMESTAMPTZ,\n    source_updated_at TIMESTAMPTZ,\n    captured_at TIMESTAMPTZ,\n    effective_at TIMESTAMPTZ,\n    tags TEXT[] DEFAULT '{}',\n    status VARCHAR DEFAULT 'active',\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    "CREATE TABLE IF NOT EXISTS summary_nodes (\n    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    summary_of VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    perspective_label TEXT,\n    perspective_instruction TEXT,\n    perspective_embedding vector(1536),\n    body TEXT,\n    body_embedding vector(1536),\n    is_default BOOLEAN DEFAULT false,\n    source JSONB DEFAULT '{}',\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    "CREATE TABLE IF NOT EXISTS entity_nodes (\n    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    canonical_name TEXT,\n    aliases TEXT[] DEFAULT '{}',\n    entity_type VARCHAR,\n    merged_into VARCHAR REFERENCES knowledge_nodes(id),\n    abstract_stale BOOLEAN DEFAULT false,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    'CREATE TABLE IF NOT EXISTS index_nodes (\n    node_id VARCHAR PRIMARY KEY REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    description TEXT,\n    rollup_instruction TEXT,\n    abstract_stale BOOLEAN DEFAULT false,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)',
    "CREATE TABLE IF NOT EXISTS index_children (\n    index_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    child_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    position INT DEFAULT 0,\n    child_role VARCHAR DEFAULT 'member',\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW(),\n    PRIMARY KEY (index_id, child_id)\n)",
    'CREATE TABLE IF NOT EXISTS entity_facts (\n    id SERIAL PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    entity_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    article_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    source_item_id VARCHAR,\n    fact_text TEXT NOT NULL,\n    fact_time TIMESTAMPTZ,\n    source_published_at TIMESTAMPTZ,\n    evidence_span TEXT,\n    confidence FLOAT DEFAULT 0.5,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW(),\n    UNIQUE (entity_id, article_id, fact_text)\n)',
    "CREATE TABLE IF NOT EXISTS entity_pair_signals (\n    entity_a_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    entity_b_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE,\n    co_occurrence_count INT DEFAULT 0,\n    co_occurrence_score FLOAT DEFAULT 0,\n    embedding_similarity FLOAT DEFAULT 0,\n    graph_proximity_score FLOAT DEFAULT 0,\n    temporal_score FLOAT DEFAULT 0,\n    relatedness_score FLOAT DEFAULT 0,\n    explanation TEXT,\n    source_article_ids TEXT[] DEFAULT '{}',\n    updated_at TIMESTAMPTZ DEFAULT NOW(),\n    PRIMARY KEY (entity_a_id, entity_b_id)\n)",
    "CREATE TABLE IF NOT EXISTS jobs (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    job_type VARCHAR NOT NULL,\n    provider VARCHAR,\n    model VARCHAR,\n    payload JSONB NOT NULL DEFAULT '{}',\n    status VARCHAR NOT NULL DEFAULT 'pending',\n    priority INT DEFAULT 0,\n    idempotency_key VARCHAR,\n    attempts INT DEFAULT 0,\n    max_attempts INT DEFAULT 3,\n    result JSONB,\n    error TEXT,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    started_at TIMESTAMPTZ,\n    finished_at TIMESTAMPTZ\n)",
    "CREATE TABLE IF NOT EXISTS folders (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    parent_id VARCHAR REFERENCES folders(id) ON DELETE SET NULL,\n    name VARCHAR NOT NULL,\n    kind VARCHAR NOT NULL DEFAULT 'normal',    -- normal | stream\n    status VARCHAR NOT NULL DEFAULT 'active',  -- active | archived\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    "CREATE TABLE IF NOT EXISTS connectors (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    folder_id VARCHAR REFERENCES folders(id) ON DELETE CASCADE,\n    type VARCHAR NOT NULL,                     -- rss | wechat\n    config JSONB DEFAULT '{}',\n    status VARCHAR NOT NULL DEFAULT 'active',  -- active | inactive\n    last_fetched_at TIMESTAMPTZ,\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    'CREATE TABLE IF NOT EXISTS raw_assets (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    storage_key TEXT,\n    original_filename TEXT,\n    mime_type VARCHAR,\n    size BIGINT,\n    sha256 VARCHAR,\n    created_at TIMESTAMPTZ DEFAULT NOW()\n)',
    "CREATE TABLE IF NOT EXISTS document_instances (\n    id VARCHAR PRIMARY KEY,\n    user_id VARCHAR NOT NULL,\n    folder_id VARCHAR REFERENCES folders(id) ON DELETE SET NULL,\n    raw_asset_id VARCHAR REFERENCES raw_assets(id),\n    connector_id VARCHAR REFERENCES connectors(id) ON DELETE SET NULL,\n    display_name TEXT,\n    origin_ref TEXT,\n    origin_ref_type VARCHAR,\n    doc_kind VARCHAR,\n    status VARCHAR NOT NULL DEFAULT 'pending', -- pending | processing | succeeded | failed | ignored\n    created_at TIMESTAMPTZ DEFAULT NOW(),\n    updated_at TIMESTAMPTZ DEFAULT NOW()\n)",
    'ALTER TABLE source_items ADD COLUMN IF NOT EXISTS doc_kind VARCHAR',
    'ALTER TABLE article_nodes ADD COLUMN IF NOT EXISTS document_instance_id VARCHAR REFERENCES document_instances(id)',
    'ALTER TABLE source_items ADD COLUMN IF NOT EXISTS document_instance_id VARCHAR REFERENCES document_instances(id)',
    'CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_user_id ON knowledge_nodes(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_embedding ON knowledge_nodes\n    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)',
    'CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_object_type ON knowledge_nodes(object_type)',
    'CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_published_at ON knowledge_nodes(published_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_entity_candidates_user ON entity_candidates(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_sources_user_id ON sources(user_id)',
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_source_items_user_source_origin\n    ON source_items(user_id, source_id, origin_ref_type, origin_ref)',
    'CREATE INDEX IF NOT EXISTS idx_source_items_source_status ON source_items(source_id, status, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_source_items_user_status ON source_items(user_id, status, created_at)',
    'CREATE INDEX IF NOT EXISTS idx_article_nodes_source_item_id ON article_nodes(source_item_id)',
    'CREATE INDEX IF NOT EXISTS idx_article_nodes_knowledge_time ON article_nodes(\n    COALESCE(effective_at, source_published_at, captured_at)\n)',
    'CREATE INDEX IF NOT EXISTS idx_summary_nodes_summary_of ON summary_nodes(summary_of)',
    'CREATE INDEX IF NOT EXISTS idx_summary_nodes_body_embedding ON summary_nodes\n    USING ivfflat (body_embedding vector_cosine_ops) WITH (lists = 100)',
    'CREATE INDEX IF NOT EXISTS idx_summary_nodes_perspective_embedding ON summary_nodes\n    USING ivfflat (perspective_embedding vector_cosine_ops) WITH (lists = 100)',
    'CREATE INDEX IF NOT EXISTS idx_entity_nodes_canonical_name ON entity_nodes(canonical_name)',
    'CREATE INDEX IF NOT EXISTS idx_entity_nodes_merged_into ON entity_nodes(merged_into)',
    'CREATE INDEX IF NOT EXISTS idx_index_children_index_position ON index_children(index_id, position)',
    'CREATE INDEX IF NOT EXISTS idx_index_children_child_id ON index_children(child_id)',
    'CREATE INDEX IF NOT EXISTS idx_entity_facts_entity_time ON entity_facts(entity_id, fact_time DESC)',
    'CREATE INDEX IF NOT EXISTS idx_entity_facts_article ON entity_facts(article_id)',
    'CREATE INDEX IF NOT EXISTS idx_entity_facts_source_item ON entity_facts(source_item_id)',
    'CREATE INDEX IF NOT EXISTS idx_entity_pair_signals_a_score ON entity_pair_signals(entity_a_id, relatedness_score DESC)',
    'CREATE INDEX IF NOT EXISTS idx_entity_pair_signals_b_score ON entity_pair_signals(entity_b_id, relatedness_score DESC)',
    'CREATE INDEX IF NOT EXISTS idx_jobs_user_status_created ON jobs(user_id, status, created_at DESC)',
    'CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority DESC, created_at ASC)',
    'CREATE INDEX IF NOT EXISTS idx_jobs_user_idempotency_key\n    ON jobs(user_id, idempotency_key)\n    WHERE idempotency_key IS NOT NULL',
    'CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id)',
    'CREATE INDEX IF NOT EXISTS idx_connectors_folder ON connectors(folder_id)',
    'CREATE INDEX IF NOT EXISTS idx_connectors_user_status ON connectors(user_id, status)',
    'CREATE INDEX IF NOT EXISTS idx_raw_assets_user ON raw_assets(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_raw_assets_sha256 ON raw_assets(sha256) WHERE sha256 IS NOT NULL',
    'CREATE INDEX IF NOT EXISTS idx_document_instances_folder ON document_instances(folder_id)',
    'CREATE INDEX IF NOT EXISTS idx_document_instances_raw_asset ON document_instances(raw_asset_id)',
    'CREATE INDEX IF NOT EXISTS idx_document_instances_status ON document_instances(user_id, status)',
    'CREATE INDEX IF NOT EXISTS idx_article_nodes_document_instance ON article_nodes(document_instance_id)',
    'CREATE INDEX IF NOT EXISTS idx_source_items_document_instance ON source_items(document_instance_id)',
    "DO $$ BEGIN\n  IF NOT EXISTS (\n    SELECT 1 FROM information_schema.table_constraints\n    WHERE constraint_name = 'uq_edges_from_to_type' AND table_name = 'knowledge_edges'\n  ) THEN\n    ALTER TABLE knowledge_edges\n      ADD CONSTRAINT uq_edges_from_to_type UNIQUE (from_node_id, to_node_id, relation_type);\n  END IF;\nEND $$",
]

# Reverse dependency order for a clean teardown.
DROP_TABLES = ['document_instances', 'raw_assets', 'connectors', 'folders', 'jobs', 'entity_pair_signals', 'entity_facts', 'index_children', 'index_nodes', 'entity_nodes', 'summary_nodes', 'article_nodes', 'source_items', 'sources', 'knowledge_edges', 'entity_candidates', 'knowledge_nodes']


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in DROP_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

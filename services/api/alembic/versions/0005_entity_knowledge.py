"""Persist entity sources and complete knowledge pages.

Revision ID: 0005_entity_knowledge
Revises: 0004_reprocess_intent
"""

from alembic import op

revision = "0005_entity_knowledge"
down_revision = "0004_reprocess_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE entity_sources (
            entity_id VARCHAR NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            article_id VARCHAR NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
            user_id VARCHAR NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (entity_id, article_id)
        )
    """)
    op.execute("CREATE INDEX idx_entity_sources_article ON entity_sources(article_id)")
    op.execute("ALTER TABLE entity_nodes ADD COLUMN body_markdown TEXT")
    op.execute("ALTER TABLE entity_nodes ADD COLUMN requested_revision BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE entity_nodes ADD COLUMN published_revision BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE entity_nodes ADD COLUMN body_published_at TIMESTAMPTZ")
    op.execute("ALTER TABLE entity_nodes ADD COLUMN body_source_ids TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE article_nodes ADD COLUMN extracted_text_ref TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE article_nodes DROP COLUMN extracted_text_ref")
    op.execute("ALTER TABLE entity_nodes DROP COLUMN body_source_ids")
    op.execute("ALTER TABLE entity_nodes DROP COLUMN body_published_at")
    op.execute("ALTER TABLE entity_nodes DROP COLUMN published_revision")
    op.execute("ALTER TABLE entity_nodes DROP COLUMN requested_revision")
    op.execute("ALTER TABLE entity_nodes DROP COLUMN body_markdown")
    op.execute("DROP TABLE entity_sources")

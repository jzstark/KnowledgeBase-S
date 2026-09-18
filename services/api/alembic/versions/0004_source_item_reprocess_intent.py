"""persist explicit source-item reprocess intent

Revision ID: 0004_reprocess_intent
Revises: 0003_vector_indexes_hnsw
Create Date: 2026-09-18
"""

from alembic import op


revision = "0004_reprocess_intent"
down_revision = "0003_vector_indexes_hnsw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE source_items "
        "ADD COLUMN IF NOT EXISTS reprocess_requested_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE source_items DROP COLUMN IF EXISTS reprocess_requested_at"
    )

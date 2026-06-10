"""vector indexes: ivfflat -> hnsw

ivfflat indexes were created at lists=100 while the tables were empty, so their
centroids never reflected the data and recall degraded as it grew (they are
never retrained). HNSW needs no training, is built incrementally as rows are
inserted, and gives better recall — the right default for a corpus that grows.

Revision ID: 0003_vector_indexes_hnsw
Revises: 0002_jobs_idempotency_unique
Create Date: 2026-06-10
"""
from alembic import op

revision = "0003_vector_indexes_hnsw"
down_revision = "0002_jobs_idempotency_unique"
branch_labels = None
depends_on = None

# (index name, table, column)
VECTOR_INDEXES = [
    ("idx_knowledge_nodes_embedding", "knowledge_nodes", "embedding"),
    ("idx_summary_nodes_body_embedding", "summary_nodes", "body_embedding"),
    ("idx_summary_nodes_perspective_embedding", "summary_nodes", "perspective_embedding"),
]


def upgrade() -> None:
    for name, table, column in VECTOR_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
            f"USING hnsw ({column} vector_cosine_ops)"
        )


def downgrade() -> None:
    for name, table, column in VECTOR_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
            f"USING ivfflat ({column} vector_cosine_ops) WITH (lists = 100)"
        )

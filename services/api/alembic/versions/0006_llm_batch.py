"""Queue background model calls for the Batches API; mark entity full rebuilds.

llm_requests memoizes one model call per (owner, request_key). Owners are
resumable units of work (a job or a source item) that replay from the start
and reuse finished results. Jobs waiting for a batch use the new 'waiting'
status, which counts as active for idempotency.

Revision ID: 0006_llm_batch
Revises: 0005_entity_knowledge
"""

from alembic import op

revision = "0006_llm_batch"
down_revision = "0005_entity_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE llm_requests (
            id VARCHAR PRIMARY KEY,
            owner VARCHAR NOT NULL,
            request_key VARCHAR NOT NULL,
            params JSONB,
            status VARCHAR NOT NULL DEFAULT 'pending',
            batch_id VARCHAR,
            attempts INT NOT NULL DEFAULT 0,
            retryable BOOLEAN NOT NULL DEFAULT true,
            response JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            submitted_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            UNIQUE (owner, request_key)
        )
    """)
    op.execute("CREATE INDEX idx_llm_requests_status ON llm_requests(status)")
    op.execute("DROP INDEX IF EXISTS uq_jobs_active_idempotency")
    op.execute("""
        CREATE UNIQUE INDEX uq_jobs_active_idempotency
        ON jobs (user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL AND status IN ('pending', 'running', 'retrying', 'waiting')
    """)
    op.execute(
        "ALTER TABLE entity_nodes ADD COLUMN full_refresh_requested BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE entity_nodes DROP COLUMN full_refresh_requested")
    op.execute("UPDATE jobs SET status = 'pending' WHERE status = 'waiting'")
    op.execute("DROP INDEX IF EXISTS uq_jobs_active_idempotency")
    op.execute("""
        CREATE UNIQUE INDEX uq_jobs_active_idempotency
        ON jobs (user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL AND status IN ('pending', 'running', 'retrying')
    """)
    op.execute("DROP TABLE llm_requests")

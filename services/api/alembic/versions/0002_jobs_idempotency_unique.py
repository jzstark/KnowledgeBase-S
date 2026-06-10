"""jobs: enforce one active job per idempotency_key

Replaces the non-unique idx_jobs_user_idempotency_key with a *partial* unique
index covering only active states (pending/running/retrying). This makes the
"at most one in-flight job per key" rule a DB invariant so enqueue_job can use
INSERT ... ON CONFLICT instead of a racy read-then-insert. Terminal jobs
(succeeded/failed/cancelled) leave the index, so a key can be re-enqueued once
its previous job has finished.

Revision ID: 0002_jobs_idempotency_unique
Revises: 0001_baseline
Create Date: 2026-06-10
"""
from alembic import op

revision = "0002_jobs_idempotency_unique"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

ACTIVE = "('pending', 'running', 'retrying')"


def upgrade() -> None:
    # Defensive: collapse any pre-existing active duplicates (keep the newest)
    # so the unique index can be created.
    op.execute(
        f"""
        DELETE FROM jobs a
        USING jobs b
        WHERE a.idempotency_key IS NOT NULL
          AND a.status IN {ACTIVE}
          AND b.status IN {ACTIVE}
          AND a.user_id = b.user_id
          AND a.idempotency_key = b.idempotency_key
          AND (a.created_at < b.created_at
               OR (a.created_at = b.created_at AND a.id < b.id))
        """
    )
    op.execute("DROP INDEX IF EXISTS idx_jobs_user_idempotency_key")
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_idempotency
        ON jobs (user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL AND status IN {ACTIVE}
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_jobs_active_idempotency")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_user_idempotency_key
        ON jobs (user_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )

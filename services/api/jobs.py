import secrets
import json
from typing import Any

import database


def _job(row) -> dict[str, Any]:
    payload = row["payload"] or {}
    result = row["result"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(result, str):
        result = json.loads(result)
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "job_type": row["job_type"],
        "provider": row["provider"],
        "model": row["model"],
        "payload": payload,
        "status": row["status"],
        "priority": row["priority"],
        "idempotency_key": row["idempotency_key"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "result": result,
        "error": row["error"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }


async def enqueue_job(
    job_type: str,
    payload: dict[str, Any],
    *,
    user_id: str = "default",
    provider: str | None = None,
    model: str | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    if idempotency_key:
        existing = await database.database.fetch_one(
            """
            SELECT *
            FROM jobs
            WHERE user_id = :user_id
              AND idempotency_key = :idempotency_key
              AND status NOT IN ('succeeded', 'failed', 'cancelled')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"user_id": user_id, "idempotency_key": idempotency_key},
        )
        if existing:
            return _job(existing)

    job_id = f"job_{secrets.token_hex(8)}"
    row = await database.database.fetch_one(
        """
        INSERT INTO jobs
          (id, user_id, job_type, provider, model, payload, priority,
           idempotency_key, max_attempts)
        VALUES
          (:id, :user_id, :job_type, :provider, :model, :payload, :priority,
           :idempotency_key, :max_attempts)
        RETURNING *
        """,
        {
            "id": job_id,
            "user_id": user_id,
            "job_type": job_type,
            "provider": provider,
            "model": model,
            "payload": database.jsonb(payload),
            "priority": priority,
            "idempotency_key": idempotency_key,
            "max_attempts": max_attempts,
        },
    )
    return _job(row)


async def list_jobs(user_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}
    status_filter = ""
    if status:
        status_filter = "AND status = :status"
        params["status"] = status
    rows = await database.database.fetch_all(
        f"""
        SELECT *
        FROM jobs
        WHERE user_id = :user_id {status_filter}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        params,
    )
    return [_job(r) for r in rows]


async def get_job(job_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    params = {"id": job_id}
    user_filter = ""
    if user_id is not None:
        user_filter = "AND user_id = :user_id"
        params["user_id"] = user_id
    row = await database.database.fetch_one(
        f"SELECT * FROM jobs WHERE id = :id {user_filter}",
        params,
    )
    return _job(row) if row else None


async def cancel_job(job_id: str, user_id: str) -> dict[str, Any] | None:
    row = await database.database.fetch_one(
        """
        UPDATE jobs
        SET status = 'cancelled',
            finished_at = NOW(),
            error = NULL
        WHERE id = :id
          AND user_id = :user_id
          AND status IN ('pending', 'retrying')
        RETURNING *
        """,
        {"id": job_id, "user_id": user_id},
    )
    return _job(row) if row else await get_job(job_id, user_id)


async def retry_job(job_id: str, user_id: str) -> dict[str, Any] | None:
    row = await database.database.fetch_one(
        """
        UPDATE jobs
        SET status = 'pending',
            attempts = 0,
            error = NULL,
            result = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE id = :id
          AND user_id = :user_id
          AND status IN ('failed', 'cancelled')
        RETURNING *
        """,
        {"id": job_id, "user_id": user_id},
    )
    return _job(row) if row else await get_job(job_id, user_id)


async def claim_next_job() -> dict[str, Any] | None:
    row = await database.database.fetch_one(
        """
        UPDATE jobs
        SET status = 'running',
            attempts = attempts + 1,
            started_at = NOW(),
            finished_at = NULL,
            error = NULL
        WHERE id = (
            SELECT id
            FROM jobs
            WHERE status IN ('pending', 'retrying')
            ORDER BY priority DESC, created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """
    )
    return _job(row) if row else None


async def complete_job(job_id: str, result: dict[str, Any] | None = None) -> None:
    await database.database.execute(
        """
        UPDATE jobs
        SET status = 'succeeded',
            result = :result,
            finished_at = NOW(),
            error = NULL
        WHERE id = :id
        """,
        {"id": job_id, "result": database.jsonb(result or {})},
    )


async def fail_job(job_id: str, error: str) -> None:
    await database.database.execute(
        """
        UPDATE jobs
        SET status = CASE WHEN attempts < max_attempts THEN 'retrying' ELSE 'failed' END,
            error = :error,
            finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE NOW() END
        WHERE id = :id
        """,
        {"id": job_id, "error": error[:4000]},
    )

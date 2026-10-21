"""Run background model calls through the Message Batches API.

An owner (a job or a source item) replays its work from the start until every
model call it needs has a result. ``call`` memoizes one call per
``(owner, request_key)``: the first replay records the request and raises
``Deferred``; the job worker submits recorded requests as batches, stores the
results, and wakes the owner, whose next replay gets the stored message back.
The first recorded parameters win, so an owner must use stable keys for calls
whose prompt can drift between replays (for example retrieval context).
"""

import json
import os
import secrets
from typing import Any

import httpx
from anthropic.types import Message

import database
from settings import settings

_MAX_ATTEMPTS = 3
_FAILED_ITEM_RETENTION_DAYS = 30  # A failed source item's retry reuses finished results.
_MAX_BATCH_REQUESTS = 10_000
_MAX_BATCH_BYTES = 150 * 1024 * 1024  # API limit is 256 MB per batch
INGESTION_WORKER_URL = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")


class Deferred(Exception):
    """The owner's model results are not ready; it resumes after the batch ends."""


class LlmRequestError(Exception):
    pass


def _client():
    from kb.retrieval import claude_client
    return claude_client


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def call(owner: str | None, key: str, params: dict[str, Any]) -> Message:
    """Return the model message for this owner's call, or raise ``Deferred``.

    Calls without an owner, or with batching disabled, run immediately.
    """
    if owner is None or not settings.llm_batch.enabled:
        return await _client().messages.create(**params)
    row = await database.database.fetch_one(
        "SELECT * FROM llm_requests WHERE owner = :owner AND request_key = :key",
        {"owner": owner, "key": key},
    )
    if row is None:
        await database.database.execute(
            """INSERT INTO llm_requests (id, owner, request_key, params)
               VALUES (:id, :owner, :key, :params)
               ON CONFLICT (owner, request_key) DO NOTHING""",
            {"id": f"llm_{secrets.token_hex(12)}", "owner": owner, "key": key,
             "params": database.jsonb(params)},
        )
        raise Deferred
    if row["status"] == "succeeded":
        return Message.model_validate(_json(row["response"]))
    if row["status"] == "failed":
        if row["retryable"] and row["attempts"] < _MAX_ATTEMPTS:
            await database.database.execute(
                "UPDATE llm_requests SET status = 'pending', batch_id = NULL WHERE id = :id",
                {"id": row["id"]},
            )
            raise Deferred
        raise LlmRequestError(f"模型批处理请求失败：{row['error']}")
    raise Deferred


async def call_many(owner: str | None, calls: list[tuple[str, dict[str, Any]]]) -> list[Message]:
    """Record every call before deferring, so independent calls share one batch round."""
    messages: list[Message] = []
    deferred = False
    for key, params in calls:
        try:
            messages.append(await call(owner, key, params))
        except Deferred:
            deferred = True
    if deferred:
        raise Deferred
    return messages


async def forget(owner: str) -> None:
    """Drop an owner's memoized calls once its work is committed."""
    await database.database.execute(
        "DELETE FROM llm_requests WHERE owner = :owner AND status NOT IN ('pending', 'submitted')",
        {"owner": owner},
    )


async def _drop_abandoned() -> int:
    """Delete calls whose owner can no longer resume: finished, cancelled or deleted.

    Runs before submission so a cancelled owner's unsent calls are never paid for.
    """
    rows = await database.database.fetch_all(
        f"""DELETE FROM llm_requests r
           WHERE (r.owner LIKE 'job:%' AND NOT EXISTS (
                    SELECT 1 FROM jobs j WHERE j.id = substr(r.owner, 5)
                      AND j.status IN ('pending', 'running', 'retrying', 'waiting')))
              OR (r.owner LIKE 'source_item:%' AND NOT EXISTS (
                    SELECT 1 FROM source_items s WHERE s.id = substr(r.owner, 13)
                      AND (s.status IN ('pending', 'processing')
                           OR (s.status = 'failed'
                               AND s.updated_at > NOW() - INTERVAL '{_FAILED_ITEM_RETENTION_DAYS} days'))))
           RETURNING r.id"""
    )
    if rows:
        print(f"[llm-batch] dropped {len(rows)} abandoned request(s)", flush=True)
    return len(rows)


async def _submit_pending() -> int:
    rows = await database.database.fetch_all(
        "SELECT id, params FROM llm_requests WHERE status = 'pending' ORDER BY created_at LIMIT :limit",
        {"limit": _MAX_BATCH_REQUESTS},
    )
    requests: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        params = row["params"] if isinstance(row["params"], str) else json.dumps(row["params"])
        if requests and size + len(params) > _MAX_BATCH_BYTES:
            break
        requests.append({"custom_id": row["id"], "params": json.loads(params)})
        size += len(params)
    if not requests:
        return 0
    batch = await _client().messages.batches.create(requests=requests)
    await database.database.execute(
        """UPDATE llm_requests SET status = 'submitted', batch_id = :batch,
             attempts = attempts + 1, submitted_at = NOW()
           WHERE id = ANY(CAST(:ids AS VARCHAR[]))""",
        {"batch": batch.id, "ids": [request["custom_id"] for request in requests]},
    )
    print(f"[llm-batch] submitted {batch.id} with {len(requests)} request(s)", flush=True)
    return len(requests)


async def _collect_finished() -> list[str]:
    """Store results of ended batches; return the owners that got results."""
    batch_ids = await database.database.fetch_all(
        "SELECT DISTINCT batch_id FROM llm_requests WHERE status = 'submitted'"
    )
    owners: set[str] = set()
    for batch_row in batch_ids:
        batch_id = batch_row["batch_id"]
        batch = await _client().messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            continue
        async for item in await _client().messages.batches.results(batch_id):
            result = item.result
            if result.type == "succeeded":
                row = await database.database.fetch_one(
                    """UPDATE llm_requests SET status = 'succeeded', response = :response,
                         params = NULL, error = NULL, finished_at = NOW()
                       WHERE id = :id AND batch_id = :batch RETURNING owner""",
                    {"id": item.custom_id, "batch": batch_id,
                     "response": database.jsonb(result.message.model_dump(mode="json"))},
                )
            else:
                error_type = result.error.error.type if result.type == "errored" else result.type
                row = await database.database.fetch_one(
                    """UPDATE llm_requests SET status = 'failed', error = :error,
                         retryable = :retryable, finished_at = NOW()
                       WHERE id = :id AND batch_id = :batch RETURNING owner""",
                    {"id": item.custom_id, "batch": batch_id,
                     "error": (result.error.error.message if result.type == "errored" else result.type)[:2000],
                     "retryable": error_type != "invalid_request_error"},
                )
            if row:
                owners.add(row["owner"])
        missing = await database.database.fetch_all(
            """UPDATE llm_requests SET status = 'failed', error = 'missing from batch results',
                 finished_at = NOW()
               WHERE batch_id = :batch AND status = 'submitted' RETURNING owner""",
            {"batch": batch_id},
        )
        owners.update(row["owner"] for row in missing)
        print(f"[llm-batch] collected {batch_id}", flush=True)
    return sorted(owners)


async def _wake_jobs() -> int:
    rows = await database.database.fetch_all(
        """UPDATE jobs SET status = 'pending'
           WHERE status = 'waiting' AND NOT EXISTS (
             SELECT 1 FROM llm_requests r
             WHERE r.owner = 'job:' || jobs.id AND r.status IN ('pending', 'submitted')
           ) RETURNING id"""
    )
    return len(rows)


async def _wake_ingestion() -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{INGESTION_WORKER_URL}/trigger", timeout=5)
    except Exception as exc:
        print(f"[llm-batch] ingestion trigger failed: {exc}; items stay pending for the worker poll", flush=True)


async def dispatch() -> None:
    """One prune / submit / collect / wake cycle, run periodically by the job worker."""
    await _drop_abandoned()
    await _submit_pending()
    owners = await _collect_finished()
    await _wake_jobs()
    if any(owner.startswith("source_item:") for owner in owners):
        await _wake_ingestion()

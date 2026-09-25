"""Document processing, archive, regeneration, and deletion rules."""

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

import database
from kb.wiki import wiki_file_path

USER_ID = "default"
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
INGESTION_WORKER_URL = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")
logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def report_source_item_status(
    item_id: str,
    status: str,
    *,
    raw_snapshot_ref: str | None = None,
    extracted_text_ref: str | None = None,
    error: str | None = None,
    title: str | None = None,
    reprocess_capable: bool = False,
) -> dict[str, Any]:
    if status not in {"pending", "processing", "succeeded", "failed", "ignored", "deleted"}:
        raise LifecycleError(400, "不支持的 source item 状态")

    updates = ["status = :status", "updated_at = NOW()"]
    params: dict[str, Any] = {"id": item_id, "status": status}
    if status == "processing":
        updates.extend(["attempts = attempts + 1", "error = NULL"])
    elif status == "failed":
        updates.append("error = :error")
        params["error"] = error[:4000] if error else None
    elif status == "succeeded":
        updates.extend(["error = NULL", "reprocess_requested_at = NULL"])
    if raw_snapshot_ref is not None:
        updates.append("raw_snapshot_ref = :raw_snapshot_ref")
        params["raw_snapshot_ref"] = raw_snapshot_ref
    if extracted_text_ref is not None:
        updates.append("extracted_text_ref = :extracted_text_ref")
        params["extracted_text_ref"] = extracted_text_ref
    if title is not None:
        updates.append("title = :title")
        params["title"] = title

    where = "id = :id"
    if status not in {"ignored", "deleted"}:
        where += " AND status NOT IN ('ignored', 'deleted')"
    if status == "processing":
        where += " AND status = 'pending'"
        if not reprocess_capable:
            where += " AND reprocess_requested_at IS NULL"

    async with database.database.transaction():
        initial = await database.database.fetch_one(
            "SELECT document_instance_id FROM source_items WHERE id = :id",
            {"id": item_id},
        )
        if initial is None:
            raise LifecycleError(404, "source item 不存在")
        document_id = initial["document_instance_id"]
        if document_id:
            await database.database.fetch_one(
                "SELECT id FROM document_instances WHERE id = :id FOR UPDATE",
                {"id": document_id},
            )
        locked_item = await database.database.fetch_one(
            "SELECT document_instance_id FROM source_items WHERE id = :id FOR UPDATE",
            {"id": item_id},
        )
        if locked_item is None:
            raise LifecycleError(404, "source item 不存在")
        if locked_item["document_instance_id"] != document_id:
            raise LifecycleError(409, "source item 关联文档已变化")

        row = await database.database.fetch_one(
            f"UPDATE source_items SET {', '.join(updates)} WHERE {where} RETURNING *",
            params,
        )
        if row is None:
            current_status = await database.database.fetch_val(
                "SELECT status FROM source_items WHERE id = :id", {"id": item_id}
            )
            if status == "processing":
                raise LifecycleError(409, f"source item 当前状态为 {current_status}，不能领取")
            raise LifecycleError(409, "source item 状态已变化")
        if document_id:
            await database.database.execute(
                "UPDATE document_instances SET status = :status, "
                "display_name = COALESCE(:title, display_name), updated_at = NOW() "
                "WHERE id = :id",
                {"id": document_id, "status": status, "title": title},
            )
    return dict(row)


async def retry_source_item(item_id: str) -> dict[str, Any]:
    async with database.database.transaction():
        initial = await database.database.fetch_one(
            "SELECT document_instance_id FROM source_items WHERE id = :id",
            {"id": item_id},
        )
        if initial is None:
            raise LifecycleError(404, "failed source item 不存在")
        document_id = initial["document_instance_id"]
        if document_id:
            await database.database.fetch_one(
                "SELECT id FROM document_instances WHERE id = :id FOR UPDATE",
                {"id": document_id},
            )
        locked_item = await database.database.fetch_one(
            "SELECT document_instance_id FROM source_items WHERE id = :id FOR UPDATE",
            {"id": item_id},
        )
        if locked_item is None or locked_item["document_instance_id"] != document_id:
            raise LifecycleError(409, "source item 关联文档已变化")
        row = await database.database.fetch_one(
            "UPDATE source_items SET status = 'pending', error = NULL, updated_at = NOW() "
            "WHERE id = :id AND status = 'failed' RETURNING *",
            {"id": item_id},
        )
        if row is None:
            raise LifecycleError(404, "failed source item 不存在")
        if document_id:
            await database.database.execute(
                "UPDATE document_instances SET status = 'pending', updated_at = NOW() WHERE id = :id",
                {"id": document_id},
            )
    return dict(row)



async def _batch_ids(ids: list[str], folder_id: str, *, limit: int, action: str) -> list[str]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        raise LifecycleError(400, "至少选择一篇文档")
    if len(unique) > limit:
        raise LifecycleError(400, f"单次最多{action} {limit} 篇文档")
    folder = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :id AND user_id = :uid",
        {"id": folder_id, "uid": USER_ID},
    )
    if folder is None:
        raise LifecycleError(404, "资料夹不存在")
    return unique


async def archive_documents(ids: list[str], folder_id: str) -> dict:
    unique = await _batch_ids(ids, folder_id, limit=1000, action="归档")
    results = []
    for di_id in unique:
        try:
            result, detail = await _archive_document_instance(di_id, folder_id=folder_id)
        except Exception:
            logger.exception("failed to archive document %s", di_id)
            result, detail = "failed", "归档失败，请重试"
        results.append({"id": di_id, "status": result, "detail": detail})
    return {
        "ok": all(item["status"] != "failed" for item in results),
        "archived": sum(item["status"] == "archived" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }


async def reprocess_documents(
    ids: list[str],
    folder_id: str,
    *,
    trigger_sources: Callable[[set[str]], Awaitable[dict[str, bool]]] | None = None,
) -> dict:
    unique = await _batch_ids(ids, folder_id, limit=1000, action="重新处理")
    results = []
    for di_id in unique:
        try:
            result = await _queue_document_instance_reprocess(di_id, folder_id=folder_id)
        except Exception:
            logger.exception("failed to queue document reprocess for %s", di_id)
            result = {"id": di_id, "status": "failed", "detail": "重新生成排队失败，请重试"}
        results.append(result)
    source_ids = {item["source_id"] for item in results if item["status"] == "accepted"}
    trigger_results = await (trigger_sources or _trigger_reprocess_sources)(source_ids)
    for item in results:
        if item["status"] == "accepted":
            item["trigger_reached"] = trigger_results.get(item["source_id"], False)
            if not item["trigger_reached"]:
                item["detail"] = "已排队；worker 暂未响应，将由后台轮询继续处理"
        item.pop("http_status", None)
    return {
        "ok": all(item["status"] != "failed" for item in results),
        "accepted": sum(item["status"] == "accepted" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "triggered_sources": sum(trigger_results.values()),
        "deferred_sources": sum(not reached for reached in trigger_results.values()),
        "results": results,
    }


async def reprocess_document(di_id: str) -> dict:
    result = await _queue_document_instance_reprocess(di_id)
    if result["status"] != "accepted":
        raise LifecycleError(result["http_status"], result["detail"])
    trigger_results = await _trigger_reprocess_sources({result["source_id"]})
    return {**result, "trigger_reached": trigger_results.get(result["source_id"], False)}


async def archive_document(di_id: str) -> tuple[str, str]:
    return await _archive_document_instance(di_id)


async def preview_delete_documents(ids: list[str], folder_id: str) -> dict:
    unique = await _batch_ids(ids, folder_id, limit=50, action="永久删除")
    return await _build_delete_preview(folder_id, unique)


async def delete_documents(ids: list[str], folder_id: str, confirmation_token: str) -> dict:
    unique = await _batch_ids(ids, folder_id, limit=50, action="永久删除")
    preview = await _build_delete_preview(folder_id, unique)
    if preview["confirmation_token"] != confirmation_token:
        raise LifecycleError(
            409,
            {
                "code": "impact_changed",
                "message": "删除范围或文档状态已变化，请重新确认",
                "preview": preview,
            },
        )
    results = []
    for di_id in unique:
        try:
            result = await _hard_delete_document_instance(di_id, folder_id=folder_id)
        except Exception:
            logger.exception("failed to permanently delete document %s", di_id)
            result = {
                "id": di_id,
                "status": "failed",
                "detail": "数据库删除失败，请核实状态后重试",
                "file_warnings": [],
            }
        results.append(result)
    warnings = [warning for item in results for warning in item["file_warnings"]]
    return {
        "ok": all(item["status"] != "failed" for item in results) and not warnings,
        "deleted": sum(item["status"] == "deleted" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "file_warnings": warnings,
        "results": results,
    }


async def delete_document(di_id: str) -> dict:
    return await _hard_delete_document_instance(di_id)


async def _archive_document_instance(
    di_id: str,
    *,
    folder_id: str | None = None,
) -> tuple[str, str]:
    """Archive one document and its source item without deleting derived knowledge."""
    async with database.database.transaction():
        row = await database.database.fetch_one(
            """
            SELECT id, folder_id, status
            FROM document_instances
            WHERE id = :id AND user_id = :uid
            FOR UPDATE
            """,
            {"id": di_id, "uid": USER_ID},
        )
        if not row:
            return "failed", "文档不存在"
        if folder_id is not None and row["folder_id"] != folder_id:
            return "failed", "文档不属于当前资料夹"
        if row["status"] == "deleted":
            return "failed", "文档已永久删除"

        source_items = await database.database.fetch_all(
            """
            SELECT id, status
            FROM source_items
            WHERE document_instance_id = :id
            FOR UPDATE
            """,
            {"id": di_id},
        )
        if row["status"] == "processing" or any(
            item["status"] == "processing" for item in source_items
        ):
            return "failed", "文档正在处理，请完成后再归档"

        already_archived = row["status"] == "ignored" and all(
            item["status"] in {"ignored", "deleted"} for item in source_items
        )
        await database.database.execute(
            """
            UPDATE source_items
            SET status = 'ignored', updated_at = NOW()
            WHERE document_instance_id = :id AND status <> 'deleted'
            """,
            {"id": di_id},
        )
        await database.database.execute(
            """
            UPDATE document_instances
            SET status = 'ignored', updated_at = NOW()
            WHERE id = :id
            """,
            {"id": di_id},
        )
        if already_archived:
            return "skipped", "文档已经归档"
        return "archived", "归档成功"


def _managed_file_path(ref: str | None) -> Path | None:
    """Resolve a stored reference only when it points inside user_data."""
    if not ref:
        return None
    try:
        path = Path(ref).resolve()
    except (OSError, ValueError):
        return None
    return path if path.is_relative_to(USER_DATA_DIR.resolve()) else None


async def _document_file_paths(di_ids: set[str]) -> set[Path]:
    if not di_ids:
        return set()
    rows = await database.database.fetch_all(
        """
        SELECT ra.storage_key, si.raw_snapshot_ref, si.extracted_text_ref
        FROM document_instances di
        LEFT JOIN raw_assets ra ON ra.id = di.raw_asset_id
        LEFT JOIN source_items si ON si.document_instance_id = di.id
        WHERE di.id = ANY(:ids)
        """,
        {"ids": list(di_ids)},
    )
    paths: set[Path] = set()
    for row in rows:
        for ref in (row["storage_key"], row["raw_snapshot_ref"], row["extracted_text_ref"]):
            path = _managed_file_path(ref)
            if path and path.is_file():
                paths.add(path)
    return paths


async def _shared_file_paths(paths: set[Path], excluded_di_ids: set[str]) -> set[Path]:
    """Return candidate paths still referenced by a non-deleted document/item."""
    if not paths:
        return set()
    rows = await database.database.fetch_all(
        """
        SELECT ra.storage_key AS ref
        FROM document_instances di
        JOIN raw_assets ra ON ra.id = di.raw_asset_id
        WHERE di.status <> 'deleted'
          AND NOT (di.id = ANY(:excluded_ids))
        UNION ALL
        SELECT si.raw_snapshot_ref AS ref
        FROM source_items si
        WHERE si.status <> 'deleted'
          AND (si.document_instance_id IS NULL
               OR NOT (si.document_instance_id = ANY(:excluded_ids)))
        UNION ALL
        SELECT si.extracted_text_ref AS ref
        FROM source_items si
        WHERE si.status <> 'deleted'
          AND (si.document_instance_id IS NULL
               OR NOT (si.document_instance_id = ANY(:excluded_ids)))
        """,
        {"excluded_ids": list(excluded_di_ids)},
    )
    referenced = {
        path
        for row in rows
        if (path := _managed_file_path(row["ref"])) is not None
    }
    return paths & referenced


async def _delete_impact(
    di_id: str,
    *,
    folder_id: str,
) -> dict:
    di = await database.database.fetch_one(
        """
        SELECT id, folder_id, display_name, origin_ref, status
        FROM document_instances
        WHERE id = :id AND user_id = :uid
        """,
        {"id": di_id, "uid": USER_ID},
    )
    if not di:
        return {
            "id": di_id, "name": di_id, "status": "failed",
            "detail": "文档不存在", "articles": 0, "summaries": 0,
            "removable_files": 0, "shared_files": 0,
        }
    name = di["display_name"] or di["origin_ref"] or di_id
    if di["folder_id"] != folder_id:
        return {
            "id": di_id, "name": name, "status": "failed",
            "detail": "文档不属于当前资料夹", "articles": 0, "summaries": 0,
            "removable_files": 0, "shared_files": 0,
        }
    if di["status"] == "deleted":
        return {
            "id": di_id, "name": name, "status": "skipped",
            "detail": "文档已经永久删除", "articles": 0, "summaries": 0,
            "removable_files": 0, "shared_files": 0,
        }
    source_rows = await database.database.fetch_all(
        "SELECT id, status FROM source_items WHERE document_instance_id = :id",
        {"id": di_id},
    )
    if di["status"] == "processing" or any(
        row["status"] == "processing" for row in source_rows
    ):
        impact_status = "blocked"
        detail = "文档正在处理，不能永久删除"
    else:
        impact_status = "eligible"
        detail = "可永久删除"
    article_rows = await database.database.fetch_all(
        """
        SELECT DISTINCT an.node_id
        FROM article_nodes an
        WHERE an.document_instance_id = :di_id
           OR an.source_item_id IN (
                SELECT id FROM source_items WHERE document_instance_id = :di_id
           )
        """,
        {"di_id": di_id},
    )
    article_ids = [row["node_id"] for row in article_rows]
    summary_count = 0
    if article_ids:
        summary_count = int(await database.database.fetch_val(
            "SELECT COUNT(*) FROM summary_nodes WHERE summary_of = ANY(:article_ids)",
            {"article_ids": article_ids},
        ) or 0)
    return {
        "id": di_id,
        "name": name,
        "status": impact_status,
        "detail": detail,
        "articles": len(article_ids),
        "summaries": summary_count,
        "removable_files": 0,
        "shared_files": 0,
    }


async def _build_delete_preview(folder_id: str, ids: list[str]) -> dict:
    unique_ids = list(dict.fromkeys(ids))
    results = [
        await _delete_impact(di_id, folder_id=folder_id)
        for di_id in unique_ids
    ]
    eligible_ids = {item["id"] for item in results if item["status"] == "eligible"}
    for item in results:
        if item["status"] != "eligible":
            continue
        item_paths = await _document_file_paths({item["id"]})
        item_shared_paths = await _shared_file_paths(item_paths, eligible_ids)
        item["removable_files"] = len(item_paths - item_shared_paths)
        item["shared_files"] = len(item_shared_paths)
    batch_paths = await _document_file_paths(eligible_ids)
    shared_paths = await _shared_file_paths(batch_paths, eligible_ids)
    fingerprint_payload = [
        {
            key: item[key]
            for key in (
                "id", "status", "articles", "summaries",
                "removable_files", "shared_files",
            )
        }
        for item in results
    ]
    token = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "confirmation_token": token,
        "documents": len(results),
        "eligible": sum(item["status"] == "eligible" for item in results),
        "blocked": sum(item["status"] == "blocked" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "articles": sum(
            item["articles"] for item in results if item["status"] == "eligible"
        ),
        "summaries": sum(
            item["summaries"] for item in results if item["status"] == "eligible"
        ),
        "removable_files": len(batch_paths - shared_paths),
        "shared_files": len(shared_paths),
        "results": results,
    }


async def _hard_delete_document_instance(
    di_id: str,
    *,
    folder_id: str | None = None,
) -> dict:
    """Atomically delete DB derivatives, then best-effort delete unshared files."""
    raw_paths = await _document_file_paths({di_id})
    wiki_paths: set[Path] = set()
    pending_cleanup_paths: set[Path] = set()
    already_deleted = False
    article_ids: list[str] = []
    summary_ids: list[str] = []

    async with database.database.transaction():
        di = await database.database.fetch_one(
            """
            SELECT id, folder_id, status
            FROM document_instances
            WHERE id = :id AND user_id = :uid
            FOR UPDATE
            """,
            {"id": di_id, "uid": USER_ID},
        )
        if not di:
            return {"id": di_id, "status": "failed", "detail": "文档不存在", "file_warnings": []}
        if folder_id is not None and di["folder_id"] != folder_id:
            return {
                "id": di_id, "status": "failed",
                "detail": "文档不属于当前资料夹", "file_warnings": [],
            }
        source_rows = await database.database.fetch_all(
            """
            SELECT id, status, error
            FROM source_items
            WHERE document_instance_id = :id
            FOR UPDATE
            """,
            {"id": di_id},
        )
        for row in source_rows:
            error = row["error"] or ""
            if not error.startswith("file_cleanup_pending:"):
                continue
            try:
                refs = json.loads(error.removeprefix("file_cleanup_pending:"))
            except (json.JSONDecodeError, TypeError):
                refs = []
            for ref in refs if isinstance(refs, list) else []:
                path = _managed_file_path(ref)
                if path:
                    pending_cleanup_paths.add(path)
        already_deleted = di["status"] == "deleted"
        if not already_deleted and (
            di["status"] == "processing" or any(
                row["status"] == "processing" for row in source_rows
            )
        ):
            return {
                "id": di_id, "status": "failed",
                "detail": "文档正在处理，不能永久删除", "file_warnings": [],
            }
        if not already_deleted:
            article_rows = await database.database.fetch_all(
                """
                SELECT DISTINCT an.node_id
                FROM article_nodes an
                WHERE an.document_instance_id = :di_id
                   OR an.source_item_id IN (
                        SELECT id FROM source_items WHERE document_instance_id = :di_id
                   )
                """,
                {"di_id": di_id},
            )
            article_ids = [row["node_id"] for row in article_rows]
        if not already_deleted and article_ids:
            summary_rows = await database.database.fetch_all(
                "SELECT node_id FROM summary_nodes WHERE summary_of = ANY(:article_ids)",
                {"article_ids": article_ids},
            )
            summary_ids = [row["node_id"] for row in summary_rows]
            entity_rows = await database.database.fetch_all(
                "SELECT DISTINCT entity_id FROM entity_facts WHERE article_id = ANY(:article_ids)",
                {"article_ids": article_ids},
            )
            entity_ids = [row["entity_id"] for row in entity_rows]
            if entity_ids:
                await database.database.execute(
                    "UPDATE entity_nodes SET abstract_stale = true, updated_at = NOW() "
                    "WHERE node_id = ANY(:entity_ids)",
                    {"entity_ids": entity_ids},
                )
            for article_id in article_ids:
                await database.database.execute(
                    """
                    UPDATE entity_candidates
                    SET source_article_ids = array_remove(
                            COALESCE(source_article_ids, '{}'::text[]), :article_id),
                        mention_count = GREATEST(COALESCE(mention_count, 0) - 1, 0),
                        updated_at = NOW()
                    WHERE :article_id = ANY(COALESCE(source_article_ids, '{}'::text[]))
                    """,
                    {"article_id": article_id},
                )
                await database.database.execute(
                    """
                    UPDATE entity_pair_signals
                    SET source_article_ids = array_remove(
                            COALESCE(source_article_ids, '{}'::text[]), :article_id),
                        co_occurrence_count = GREATEST(COALESCE(co_occurrence_count, 0) - 1, 0),
                        updated_at = NOW()
                    WHERE :article_id = ANY(COALESCE(source_article_ids, '{}'::text[]))
                    """,
                    {"article_id": article_id},
                )
            await database.database.execute(
                "DELETE FROM entity_candidates WHERE promoted_entity_id IS NULL "
                "AND cardinality(COALESCE(source_article_ids, '{}'::text[])) = 0"
            )
            await database.database.execute(
                "DELETE FROM entity_pair_signals "
                "WHERE cardinality(COALESCE(source_article_ids, '{}'::text[])) = 0"
            )
        if not already_deleted and summary_ids:
            await database.database.execute(
                "DELETE FROM knowledge_nodes WHERE id = ANY(:ids)", {"ids": summary_ids}
            )
        if not already_deleted and article_ids:
            await database.database.execute(
                "DELETE FROM knowledge_nodes WHERE id = ANY(:ids)", {"ids": article_ids}
            )
        if not already_deleted:
            await database.database.execute(
                """
                UPDATE source_items
                SET status = 'deleted', error = NULL, reprocess_requested_at = NULL,
                    updated_at = NOW()
                WHERE document_instance_id = :id
                """,
                {"id": di_id},
            )
            await database.database.execute(
                "UPDATE document_instances SET status = 'deleted', updated_at = NOW() WHERE id = :id",
                {"id": di_id},
            )
            wiki_paths.update(
                wiki_file_path(USER_ID, node_id, "article") for node_id in article_ids
            )
            wiki_paths.update(
                wiki_file_path(USER_ID, node_id, "summary") for node_id in summary_ids
            )

    # Re-check shared references after the DB commit.  Another document may have
    # begun referencing the same asset while this deletion was running.
    cleanup_paths = raw_paths | wiki_paths | pending_cleanup_paths
    shared_paths = await _shared_file_paths(cleanup_paths, {di_id})
    warnings = []
    failed_paths = []
    for path in sorted(cleanup_paths - shared_paths):
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            warnings.append(f"{path.name}: {exc}")
            failed_paths.append(path)
    try:
        pending_error = (
            "file_cleanup_pending:" + json.dumps([str(path) for path in failed_paths])
            if failed_paths else None
        )
        await database.database.execute(
            "UPDATE source_items SET error = :error, updated_at = NOW() "
            "WHERE document_instance_id = :id AND status = 'deleted'",
            {"id": di_id, "error": pending_error},
        )
    except Exception as exc:
        warnings.append(f"文件清理状态记录失败: {exc}")
    return {
        "id": di_id,
        "status": "skipped" if already_deleted else "deleted",
        "detail": (
            "文档已删除，遗留文件清理完成" if already_deleted and not warnings
            else "文档已删除，但遗留文件清理仍失败" if already_deleted
            else "永久删除成功" if not warnings
            else "数据库已删除，但部分文件清理失败"
        ),
        "articles": len(article_ids),
        "summaries": len(summary_ids),
        "shared_files_preserved": len(shared_paths),
        "file_warnings": warnings,
    }


async def _queue_document_instance_reprocess(
    di_id: str,
    *,
    folder_id: str | None = None,
) -> dict:
    """Persist one retry/regeneration request without triggering the worker."""
    async with database.database.transaction():
        di = await database.database.fetch_one(
            """
            SELECT id, folder_id, status
            FROM document_instances
            WHERE id = :id AND user_id = :uid
            FOR UPDATE
            """,
            {"id": di_id, "uid": USER_ID},
        )
        if not di:
            return {
                "id": di_id,
                "status": "failed",
                "detail": "文档实例不存在",
                "http_status": 404,
            }
        if folder_id is not None and di["folder_id"] != folder_id:
            return {
                "id": di_id,
                "status": "failed",
                "detail": "文档不属于当前资料夹",
                "http_status": 409,
            }
        if di["status"] == "processing":
            return {
                "id": di_id,
                "status": "skipped",
                "detail": "文档正在处理，已跳过",
                "http_status": 409,
            }
        if di["status"] == "pending":
            return {
                "id": di_id,
                "status": "skipped",
                "detail": "文档已经排队，已跳过",
                "http_status": 409,
            }
        if di["status"] in {"ignored", "deleted"}:
            return {
                "id": di_id,
                "status": "skipped",
                "detail": "已归档或删除的文档不能重新处理",
                "http_status": 409,
            }

        source_items = await database.database.fetch_all(
            """
            SELECT si.*, s.deleted_at AS source_deleted_at
            FROM source_items si
            LEFT JOIN sources s ON s.id = si.source_id
            WHERE si.document_instance_id = :di_id
            ORDER BY si.created_at ASC, si.id ASC
            FOR UPDATE OF si
            """,
            {"di_id": di_id},
        )
        if not source_items:
            return {
                "id": di_id,
                "status": "failed",
                "detail": "文档缺少关联 source item，无法重新处理",
                "http_status": 409,
            }
        if any(item["status"] == "processing" for item in source_items):
            return {
                "id": di_id,
                "status": "skipped",
                "detail": "文档正在处理，已跳过",
                "http_status": 409,
            }
        if any(item["status"] == "pending" for item in source_items):
            return {
                "id": di_id,
                "status": "skipped",
                "detail": "文档已经排队，已跳过",
                "http_status": 409,
            }

        article_rows = await database.database.fetch_all(
            """
            SELECT DISTINCT an.node_id, an.source_item_id
            FROM article_nodes an
            WHERE an.document_instance_id = :di_id
               OR an.source_item_id IN (
                    SELECT id FROM source_items WHERE document_instance_id = :di_id
               )
            ORDER BY an.node_id
            """,
            {"di_id": di_id},
        )
        if len(article_rows) > 1:
            return {
                "id": di_id,
                "status": "failed",
                "detail": "该文档关联多篇文章，暂不支持单篇重新生成",
                "http_status": 409,
            }

        preferred_source_item_id = (
            article_rows[0]["source_item_id"] if article_rows else None
        )
        source_item = next(
            (
                item
                for item in source_items
                if item["id"] == preferred_source_item_id
            ),
            source_items[0],
        )
        if source_item["status"] in {"ignored", "deleted"}:
            return {
                "id": di_id,
                "status": "skipped",
                "detail": "关联 source item 已归档或删除",
                "http_status": 409,
            }
        if not source_item["source_id"] or source_item["source_deleted_at"] is not None:
            return {
                "id": di_id,
                "status": "failed",
                "detail": "关联来源不存在或已删除",
                "http_status": 409,
            }

        raw_snapshot_ref = source_item["raw_snapshot_ref"]
        has_snapshot = bool(raw_snapshot_ref and Path(raw_snapshot_ref).is_file())
        origin_ref = (source_item["origin_ref"] or "").strip()
        can_refetch_url = source_item["origin_ref_type"] in {"url", "feed_entry"} and bool(origin_ref)
        if not has_snapshot and not can_refetch_url:
            return {
                "id": di_id,
                "status": "failed",
                "detail": "缺少可用原文或来源链接，无法重新处理",
                "http_status": 409,
            }

        regenerate = bool(article_rows)
        await database.database.execute(
            """
            UPDATE source_items
            SET status = 'pending',
                error = NULL,
                attempts = 0,
                reprocess_requested_at = CASE WHEN :regenerate THEN NOW() ELSE NULL END,
                updated_at = NOW()
            WHERE id = :id
            """,
            {"id": source_item["id"], "regenerate": regenerate},
        )
        await database.database.execute(
            """
            UPDATE document_instances
            SET status = 'pending', updated_at = NOW()
            WHERE id = :id
            """,
            {"id": di_id},
        )

    return {
        "id": di_id,
        "status": "accepted",
        "detail": "已排队，等待 worker 处理",
        "mode": "regenerate" if regenerate else "retry",
        "document_instance_id": di_id,
        "source_item_id": source_item["id"],
        "source_id": source_item["source_id"],
    }


async def _trigger_reprocess_sources(source_ids: set[str]) -> dict[str, bool]:
    """Trigger each real source once; pending polling remains the fallback."""
    async def trigger_one(client: httpx.AsyncClient, source_id: str) -> tuple[str, bool]:
        try:
            response = await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5
            )
            response.raise_for_status()
            return source_id, True
        except Exception as exc:
            logger.warning(
                "ingestion trigger failed for source %s: %s; pending poll will retry",
                source_id,
                exc,
            )
            return source_id, False

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(trigger_one(client, source_id) for source_id in sorted(source_ids))
        )
    return dict(results)

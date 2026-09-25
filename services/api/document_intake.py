"""Create and link raw assets, document instances, and source items."""

import hashlib
import logging
import secrets
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import database

logger = logging.getLogger(__name__)
Trigger = Callable[[str], Awaitable[bool | None]]


def _write_new_file(path: Path, content: bytes) -> None:
    created = False
    try:
        with path.open("xb") as output:
            created = True
            output.write(content)
    except Exception:
        if created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to clean up partial upload %s", path)
                raise
        raise


class IntakeError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _mapped_folder_id(source_id: str) -> str | None:
    return "fld_" + source_id[4:] if source_id.startswith("src_") else None


def _mapped_connector_id(source_id: str) -> str | None:
    return "con_" + source_id[4:] if source_id.startswith("src_") else None


async def _lock_existing_item(source: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    key = {"uid": source["user_id"], "source_id": source["id"],
           "origin_ref_type": item["origin_ref_type"], "origin_ref": item["origin_ref"]}
    existing = await database.database.fetch_one(
        """
        SELECT id, document_instance_id FROM source_items
        WHERE user_id = :uid AND source_id = :source_id
          AND origin_ref_type = :origin_ref_type AND origin_ref = :origin_ref
        """,
        key,
    )
    if existing is None:
        return None
    document_id = existing["document_instance_id"]
    mapped_document_id = document_id or f"di_{existing['id'][3:]}"
    if mapped_document_id:
        document = await database.database.fetch_one(
            "SELECT id FROM document_instances WHERE id = :id FOR UPDATE", {"id": mapped_document_id}
        )
        if document_id and document is None:
            raise IntakeError(409, "来源条目关联的文档不存在")
        if not document_id and document is not None:
            raise IntakeError(409, "来源条目存在未关联的历史文档，请检查")
    if not document_id:
        raw_asset = await database.database.fetch_one(
            "SELECT id FROM raw_assets WHERE id = :id", {"id": f"ra_{existing['id'][3:]}"}
        )
        if raw_asset is not None:
            raise IntakeError(409, "来源条目存在未关联的历史原始材料，请检查")
    locked = await database.database.fetch_one(
        "SELECT * FROM source_items WHERE id = :id FOR UPDATE", {"id": existing["id"]}
    )
    if locked is None or locked["document_instance_id"] != document_id:
        raise IntakeError(409, "来源条目的文档关联已变化，请重试")
    return dict(locked)


async def _materialize_source_document(
    source: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    folder_id = _mapped_folder_id(source["id"])
    if not folder_id:
        return item
    folder = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :id AND user_id = :uid",
        {"id": folder_id, "uid": source["user_id"]},
    )
    if not folder:
        return item

    suffix = item["id"][3:]
    raw_asset_id, document_id = f"ra_{suffix}", f"di_{suffix}"
    connector_id = None
    if source.get("type") in ("rss", "wechat") and source.get("fetch_mode") == "subscription":
        candidate = _mapped_connector_id(source["id"])
        connector = await database.database.fetch_one(
            "SELECT id FROM connectors WHERE id = :id AND user_id = :uid",
            {"id": candidate, "uid": source["user_id"]},
        )
        if connector:
            connector_id = candidate

    await database.database.execute(
        """
        INSERT INTO raw_assets (id, user_id, storage_key, original_filename, mime_type, sha256, created_at)
        VALUES (:id, :uid, :storage_key, :filename, 'text/html', :sha256, NOW())
        """,
        {"id": raw_asset_id, "uid": source["user_id"],
         "storage_key": item.get("raw_snapshot_ref") or item.get("extracted_text_ref") or item["origin_ref"],
         "filename": item.get("title"), "sha256": item.get("content_hash")},
    )
    await database.database.execute(
        """
        INSERT INTO document_instances
          (id, user_id, folder_id, raw_asset_id, connector_id,
           display_name, origin_ref, origin_ref_type, doc_kind, status, created_at, updated_at)
        VALUES
          (:id, :uid, :folder_id, :raw_asset_id, :connector_id,
           :display_name, :origin_ref, :origin_ref_type, :doc_kind, :status, NOW(), NOW())
        """,
        {"id": document_id, "uid": source["user_id"], "folder_id": folder_id,
         "raw_asset_id": raw_asset_id, "connector_id": connector_id,
         "display_name": item.get("title") or item["origin_ref"],
         "origin_ref": item["origin_ref"], "origin_ref_type": item["origin_ref_type"],
         "doc_kind": item.get("doc_kind") or source.get("default_doc_kind"),
         "status": item.get("status") or "pending"},
    )
    updated = await database.database.fetch_one(
        """
        UPDATE source_items SET document_instance_id = :document_id, updated_at = NOW()
        WHERE id = :id AND document_instance_id IS NULL RETURNING *
        """,
        {"id": item["id"], "document_id": document_id},
    )
    if updated is None:
        raise IntakeError(409, "来源条目的文档关联已变化，请重试")
    return dict(updated)


async def _receive_source_item(
    source_row: Any, input_item: dict[str, Any], *, folder_url_id: str | None = None
) -> tuple[dict[str, Any], bool]:
    source = dict(source_row)
    if not input_item.get("origin_ref"):
        raise IntakeError(400, "origin_ref 不能为空")
    item = dict(input_item)
    async with database.database.transaction():
        existing = await _lock_existing_item(source, item)
        was_existing = existing is not None
        if existing is None:
            new_id = f"si_{secrets.token_hex(8)}"
            existing_row = await database.database.fetch_one(
                """
                INSERT INTO source_items
                  (id, user_id, source_id, source_type, origin_ref, origin_ref_type,
                   raw_snapshot_ref, extracted_text_ref, content_hash, title,
                   source_published_at, source_updated_at, captured_at, effective_at,
                   doc_kind, raw_retention_policy, status)
                VALUES
                  (:id, :user_id, :source_id, :source_type, :origin_ref, :origin_ref_type,
                   :raw_snapshot_ref, :extracted_text_ref, :content_hash, :title,
                   :source_published_at, :source_updated_at, :captured_at, :effective_at,
                   :doc_kind, :raw_retention_policy, :status)
                ON CONFLICT (user_id, source_id, origin_ref_type, origin_ref) DO NOTHING
                RETURNING *
                """,
                {"id": new_id, "user_id": source["user_id"], "source_id": source["id"],
                 "source_type": source["type"], "origin_ref": item["origin_ref"],
                 "origin_ref_type": item["origin_ref_type"],
                 "raw_snapshot_ref": item.get("raw_snapshot_ref"),
                 "extracted_text_ref": item.get("extracted_text_ref"),
                 "content_hash": item.get("content_hash"), "title": item.get("title"),
                 "source_published_at": item.get("source_published_at"),
                 "source_updated_at": item.get("source_updated_at"),
                 "captured_at": item.get("captured_at"), "effective_at": item.get("effective_at"),
                 "doc_kind": item.get("doc_kind"),
                 "raw_retention_policy": item.get("raw_retention_policy", "keep_extracted_only"),
                 "status": item.get("status", "pending")},
            )
            if existing_row is None:
                existing = await _lock_existing_item(source, item)
                if existing is None:
                    raise IntakeError(409, "来源条目并发创建后不可见，请重试")
                was_existing = True
            else:
                existing = dict(existing_row)
        elif not folder_url_id:
            existing = await _update_existing_source_item(existing, item)

        if folder_url_id and was_existing:
            if existing["status"] in ("ignored", "deleted"):
                raise IntakeError(409, "URL 已归档或删除，请使用已有文档的明确操作")
            if existing["document_instance_id"]:
                document = await database.database.fetch_one(
                    "SELECT folder_id, status FROM document_instances WHERE id = :id",
                    {"id": existing["document_instance_id"]},
                )
                if document and document["folder_id"] != folder_url_id:
                    raise IntakeError(409, "URL 对应的文档已移出此资料夹")
                if document and document["status"] in ("ignored", "deleted"):
                    raise IntakeError(409, "URL 对应的文档已归档或删除，请使用已有文档的明确操作")
        if existing["document_instance_id"] or (was_existing and existing["status"] in ("ignored", "deleted")):
            return existing, not was_existing
        materialized = await _materialize_source_document(source, existing)
        if folder_url_id and not materialized["document_instance_id"]:
            raise IntakeError(409, "资料夹已不可用，请重试")
        queued = materialized["status"] == "pending" and (
            not was_existing or bool(materialized["document_instance_id"])
        )
        return materialized, queued


async def receive_source_item(source_row: Any, input_item: dict[str, Any]) -> dict[str, Any]:
    result, _ = await _receive_source_item(source_row, input_item)
    return result


async def receive_folder_url(
    source_row: Any, folder_id: str, url: str, doc_kind: str | None
) -> tuple[dict[str, str], bool]:
    source = dict(source_row)
    source["type"] = "url"
    item, queued = await _receive_source_item(
        source,
        {"origin_ref": url, "origin_ref_type": "url",
         "content_hash": hashlib.sha256(url.encode()).hexdigest(),
         "title": url, "captured_at": datetime.now(timezone.utc), "doc_kind": doc_kind},
        folder_url_id=folder_id,
    )
    if not item["document_instance_id"]:
        raise IntakeError(409, "URL 已存在，但缺少关联文档，请检查")
    return {"document_instance_id": item["document_instance_id"],
            "source_item_id": item["id"]}, queued


async def _update_existing_source_item(existing: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    # Repeated intake refreshes provenance metadata, never processing state or linkage.
    updated = await database.database.fetch_one(
        """
        UPDATE source_items SET
          raw_snapshot_ref = COALESCE(:raw_snapshot_ref, raw_snapshot_ref),
          extracted_text_ref = COALESCE(:extracted_text_ref, extracted_text_ref),
          content_hash = COALESCE(:content_hash, content_hash),
          title = COALESCE(:title, title),
          source_published_at = COALESCE(:source_published_at, source_published_at),
          source_updated_at = COALESCE(:source_updated_at, source_updated_at),
          captured_at = COALESCE(:captured_at, captured_at),
          effective_at = COALESCE(:effective_at, effective_at),
          doc_kind = COALESCE(:doc_kind, doc_kind),
          raw_retention_policy = COALESCE(:raw_retention_policy, raw_retention_policy),
          updated_at = NOW()
        WHERE id = :id RETURNING *
        """,
        {"id": existing["id"], "raw_snapshot_ref": item.get("raw_snapshot_ref"),
         "extracted_text_ref": item.get("extracted_text_ref"),
         "content_hash": item.get("content_hash"), "title": item.get("title"),
         "source_published_at": item.get("source_published_at"),
         "source_updated_at": item.get("source_updated_at"),
         "captured_at": item.get("captured_at"), "effective_at": item.get("effective_at"),
         "doc_kind": item.get("doc_kind"),
         "raw_retention_policy": item.get("raw_retention_policy")},
    )
    assert updated is not None
    return dict(updated)


async def receive_folder_upload(
    *,
    user_id: str,
    folder_id: str,
    source_id: str,
    source_type: str,
    mime_type: str,
    filename: str | None,
    content: bytes,
    captured_at: datetime,
    effective_at: datetime,
    doc_kind: str | None,
    storage_root: Path,
) -> dict[str, str]:
    raw_dir = storage_root / user_id / "raw" / source_type
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(4)}-{filename or 'upload'}"
    file_path = raw_dir / safe_name
    suffix = secrets.token_hex(8)
    raw_asset_id, document_id, item_id = f"ra_{suffix}", f"di_{suffix}", f"si_{suffix}"
    origin_ref = f"upload://{safe_name}"
    _write_new_file(file_path, content)
    try:
        async with database.database.transaction():
            await database.database.execute(
                """
                INSERT INTO raw_assets (id, user_id, storage_key, original_filename, mime_type, size, sha256, created_at)
                VALUES (:id, :uid, :storage_key, :fname, :mime, :size, :sha256, NOW())
                """,
                {"id": raw_asset_id, "uid": user_id, "storage_key": str(file_path),
                 "fname": filename, "mime": mime_type, "size": len(content),
                 "sha256": hashlib.sha256(content).hexdigest()},
            )
            await database.database.execute(
                """
                INSERT INTO document_instances
                  (id, user_id, folder_id, raw_asset_id, display_name, origin_ref, origin_ref_type,
                   doc_kind, status, created_at, updated_at)
                VALUES (:id, :uid, :fid, :ra_id, :name, :origin_ref, 'upload', :doc_kind, 'pending', NOW(), NOW())
                """,
                {"id": document_id, "uid": user_id, "fid": folder_id, "ra_id": raw_asset_id,
                 "name": Path(filename or safe_name).stem, "origin_ref": origin_ref,
                 "doc_kind": doc_kind},
            )
            await database.database.execute(
                """
                INSERT INTO source_items
                  (id, user_id, source_id, source_type, origin_ref, origin_ref_type,
                   raw_snapshot_ref, content_hash, title, captured_at, effective_at,
                   doc_kind, raw_retention_policy, document_instance_id, status)
                VALUES
                  (:id, :uid, :source_id, :src_type, :origin_ref, 'upload',
                   :raw_snapshot_ref, :hash, :title, :cap, :eff,
                   :doc_kind, 'keep_raw', :di_id, 'pending')
                """,
                {"id": item_id, "uid": user_id, "source_id": source_id,
                 "src_type": source_type, "origin_ref": origin_ref,
                 "raw_snapshot_ref": str(file_path), "hash": hashlib.sha256(content).hexdigest(),
                 "title": Path(filename or safe_name).stem, "cap": captured_at, "eff": effective_at,
                 "doc_kind": doc_kind, "di_id": document_id},
            )
    except Exception:
        try:
            committed = await database.database.fetch_val(
                "SELECT id FROM raw_assets WHERE id = :id", {"id": raw_asset_id}
            )
        except Exception:
            logger.exception("cannot verify whether intake committed for %s; retaining file %s", raw_asset_id, file_path)
            raise
        if not committed:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to clean up uncommitted upload %s", file_path)
                raise
        raise
    return {"document_instance_id": document_id, "source_item_id": item_id}


async def receive_source_upload(
    *,
    source_row: Any,
    filename: str | None,
    content: bytes,
    captured_at: datetime,
    effective_at: datetime,
    doc_kind: str | None,
    storage_root: Path,
) -> dict[str, Any]:
    source = dict(source_row)
    raw_dir = storage_root / source["user_id"] / "raw" / source["type"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{date.today()}-{secrets.token_hex(4)}-{filename or 'upload'}"
    file_path = raw_dir / safe_name
    _write_new_file(file_path, content)
    try:
        return await receive_source_item(
            source,
            {"origin_ref": f"upload://{safe_name}", "origin_ref_type": "upload",
             "raw_snapshot_ref": str(file_path),
             "content_hash": hashlib.sha256(content).hexdigest(),
             "title": Path(filename or safe_name).stem,
             "captured_at": captured_at, "effective_at": effective_at,
             "doc_kind": doc_kind, "raw_retention_policy": "keep_raw"},
        )
    except Exception:
        try:
            committed = await database.database.fetch_val(
                "SELECT id FROM source_items WHERE raw_snapshot_ref = :path LIMIT 1",
                {"path": str(file_path)},
            )
        except Exception:
            logger.exception("cannot verify whether source upload committed; retaining file %s", file_path)
            raise
        if not committed:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("failed to clean up uncommitted source upload %s", file_path)
                raise
        raise


async def _trigger_after_commit(source_id: str, trigger: Trigger) -> bool:
    try:
        return bool(await trigger(source_id))
    except Exception:
        logger.exception("ingestion trigger failed for %s; committed items remain available for polling", source_id)
        return False


async def receive_folder_uploads(
    *,
    user_id: str,
    folder_id: str,
    source_id: str,
    uploads: AsyncIterable[tuple[str | None, bytes, str, str]],
    captured_at: datetime,
    effective_at: datetime,
    doc_kind: str | None,
    storage_root: Path,
    trigger: Trigger,
) -> list[dict[str, str]]:
    results = []
    async for filename, content, source_type, mime_type in uploads:
        results.append(await receive_folder_upload(
            user_id=user_id, folder_id=folder_id, source_id=source_id,
            source_type=source_type, mime_type=mime_type, filename=filename,
            content=content, captured_at=captured_at, effective_at=effective_at,
            doc_kind=doc_kind, storage_root=storage_root,
        ))
    if results:
        await _trigger_after_commit(source_id, trigger)
    return results


async def receive_folder_urls(
    *, source_row: Any, folder_id: str, urls: Iterable[str],
    doc_kind: str | None, trigger: Trigger,
) -> dict[str, Any]:
    source = dict(source_row)
    results = []
    queued = 0
    for url in urls:
        item, is_queued = await receive_folder_url(source, folder_id, url, doc_kind)
        results.append(item)
        queued += is_queued
    if queued:
        await _trigger_after_commit(source["id"], trigger)
    return {"ok": True, "urls_queued": queued,
            "urls_reused": len(results) - queued, "items": results}


async def receive_source_uploads(
    *,
    source_row: Any,
    uploads: AsyncIterable[tuple[str | None, bytes]],
    captured_at: datetime,
    effective_at: datetime,
    doc_kind: str | None,
    storage_root: Path,
    trigger: Trigger,
) -> tuple[list[dict[str, Any]], bool]:
    source = dict(source_row)
    results = []
    async for filename, content in uploads:
        results.append(await receive_source_upload(
            source_row=source, filename=filename, content=content,
            captured_at=captured_at, effective_at=effective_at,
            doc_kind=doc_kind, storage_root=storage_root,
        ))
    triggered = await _trigger_after_commit(source["id"], trigger) if results else False
    return results, triggered


async def receive_source_items(
    source_row: Any,
    items: Iterable[dict[str, Any]],
    *,
    trigger: Trigger | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    source = dict(source_row)
    results = []
    for item in items:
        results.append(await receive_source_item(source, item))
    triggered = await _trigger_after_commit(source["id"], trigger) if results and trigger else False
    return results, triggered


async def receive_source_urls(
    source_row: Any, urls: Iterable[str], doc_kind: str | None, trigger: Trigger
) -> tuple[list[dict[str, Any]], int, bool]:
    source = dict(source_row)
    results = []
    queued = 0
    for url in urls:
        item, is_queued = await _receive_source_item(
            source,
            {"origin_ref": url, "origin_ref_type": "url",
             "content_hash": hashlib.sha256(url.encode("utf-8")).hexdigest(),
             "captured_at": datetime.now(timezone.utc), "doc_kind": doc_kind,
             "raw_retention_policy": "keep_extracted_only"},
        )
        results.append(item)
        queued += is_queued
    triggered = await _trigger_after_commit(source["id"], trigger) if queued else False
    return results, queued, triggered

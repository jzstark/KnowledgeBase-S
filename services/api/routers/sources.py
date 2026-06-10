import hashlib
import json
import logging
import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from pydantic import BaseModel

import database
from settings import settings
from auth import require_auth, require_auth_or_service_token

router = APIRouter(prefix="/api/sources", tags=["sources"])

INGESTION_WORKER_URL = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")
WECHAT2RSS_BASE_URL = os.environ.get("WECHAT2RSS_BASE_URL", "https://rss.laughtale.co.uk/wechat-api")
WECHAT2RSS_TOKEN = os.environ.get("WECHAT2RSS_TOKEN", "")
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

logger = logging.getLogger(__name__)


async def _trigger_ingestion(source_id: str) -> bool:
    """Best-effort kick to the ingestion worker. Returns whether it was reached.

    Items are already persisted as 'pending', so a failed trigger is not fatal:
    the worker's poll loop picks up any source with pending items as a fallback
    (see GET /pending/source-ids). We log instead of swallowing silently.
    """
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5)
        return True
    except Exception as exc:
        logger.warning(
            "ingestion trigger failed for %s: %s; items remain pending for the worker poll",
            source_id,
            exc,
        )
        return False


class SourceCreate(BaseModel):
    name: str
    type: str        # 'wechat'|'rss'|'url'|'pdf'|'image'|'plaintext'|'word'|'epub'
    config: dict[str, Any] = {}
    is_primary: bool = True
    default_doc_kind: str | None = None   # 来源级默认文件类型，cascade 到 source_items


class SourceUpdate(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    is_primary: bool | None = None
    default_doc_kind: str | None = None
    last_fetched_at: str | None = None   # ISO8601，worker 回写用


def _validate_doc_kind(value: str | None) -> str | None:
    """对外接口的 doc_kind 校验：必须在 config 枚举内，否则 400。"""
    if value is None or value == "":
        return None
    allowed = set(settings.doc_kind.values)
    if allowed and value not in allowed:
        raise HTTPException(400, f"invalid doc_kind '{value}'; allowed: {sorted(allowed)}")
    return value


FETCH_MODES = {
    "wechat": "subscription",
    "rss": "subscription",
    "url": "manual",
    "pdf": "manual",
    "image": "manual",
    "plaintext": "manual",
    "word": "manual",
    "epub": "manual",
}

FILE_ACCEPT = {
    "pdf": ".pdf",
    "image": ".jpg,.jpeg,.png,.gif,.webp",
    "plaintext": ".txt,.md",
    "word": ".doc,.docx",
    "epub": ".epub,.mobi,.azw3",
}


class SourceItemCreate(BaseModel):
    origin_ref: str
    origin_ref_type: str
    raw_snapshot_ref: str | None = None
    extracted_text_ref: str | None = None
    content_hash: str | None = None
    title: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    captured_at: datetime | None = None
    effective_at: datetime | None = None
    doc_kind: str | None = None              # 单条 item 级覆盖（优先于 source.default_doc_kind）
    raw_retention_policy: str = "keep_extracted_only"
    status: str = "pending"


class SourceItemsCreate(BaseModel):
    items: list[SourceItemCreate]


class SourceItemStatusUpdate(BaseModel):
    status: str
    raw_snapshot_ref: str | None = None
    extracted_text_ref: str | None = None
    error: str | None = None
    title: str | None = None


class SourceItemUpdate(BaseModel):
    doc_kind: str | None = None


class Wechat2RSSSourceCreate(BaseModel):
    feed_id: str
    name: str | None = None
    is_primary: bool = True


def _validate_optional_time(value: str | None, field_name: str) -> str | None:
    if not value:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"{field_name} 必须是 ISO8601 时间")
    return value


def _serialize_source_item(row) -> dict[str, Any]:
    d = dict(row)
    for key in (
        "source_published_at",
        "source_updated_at",
        "captured_at",
        "effective_at",
        "created_at",
        "updated_at",
    ):
        if d.get(key):
            d[key] = d[key].isoformat()
    return d


def _mapped_folder_id(source_id: str) -> str | None:
    if not source_id.startswith("src_"):
        return None
    return "fld_" + source_id[4:]


def _mapped_connector_id(source_id: str) -> str | None:
    if not source_id.startswith("src_"):
        return None
    return "con_" + source_id[4:]


async def _ensure_document_instance_for_source_item(source_row, item_row) -> dict[str, Any]:
    source = dict(source_row)
    item = dict(item_row)
    if item.get("document_instance_id"):
        return item

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
    raw_asset_id = f"ra_{suffix}"
    document_instance_id = f"di_{suffix}"
    storage_key = item.get("raw_snapshot_ref") or item.get("extracted_text_ref") or item.get("origin_ref")
    connector_id = None
    if source.get("type") in ("rss", "wechat") and source.get("fetch_mode") == "subscription":
        candidate_connector_id = _mapped_connector_id(source["id"])
        if candidate_connector_id:
            connector = await database.database.fetch_one(
                "SELECT id FROM connectors WHERE id = :id AND user_id = :uid",
                {"id": candidate_connector_id, "uid": source["user_id"]},
            )
            if connector:
                connector_id = candidate_connector_id

    await database.database.execute(
        """
        INSERT INTO raw_assets (id, user_id, storage_key, original_filename, mime_type, sha256, created_at)
        VALUES (:id, :uid, :storage_key, :filename, 'text/html', :sha256, NOW())
        ON CONFLICT (id) DO UPDATE SET
          storage_key = COALESCE(EXCLUDED.storage_key, raw_assets.storage_key),
          original_filename = COALESCE(EXCLUDED.original_filename, raw_assets.original_filename),
          sha256 = COALESCE(EXCLUDED.sha256, raw_assets.sha256)
        """,
        {
            "id": raw_asset_id,
            "uid": source["user_id"],
            "storage_key": storage_key,
            "filename": item.get("title"),
            "sha256": item.get("content_hash"),
        },
    )
    await database.database.execute(
        """
        INSERT INTO document_instances
          (id, user_id, folder_id, raw_asset_id, connector_id,
           display_name, origin_ref, origin_ref_type, doc_kind, status, created_at, updated_at)
        VALUES
          (:id, :uid, :folder_id, :raw_asset_id, :connector_id,
           :display_name, :origin_ref, :origin_ref_type, :doc_kind, :status, NOW(), NOW())
        ON CONFLICT (id) DO UPDATE SET
          connector_id = COALESCE(EXCLUDED.connector_id, document_instances.connector_id),
          display_name = COALESCE(EXCLUDED.display_name, document_instances.display_name),
          origin_ref = COALESCE(EXCLUDED.origin_ref, document_instances.origin_ref),
          origin_ref_type = COALESCE(EXCLUDED.origin_ref_type, document_instances.origin_ref_type),
          doc_kind = COALESCE(EXCLUDED.doc_kind, document_instances.doc_kind),
          status = EXCLUDED.status,
          updated_at = NOW()
        """,
        {
            "id": document_instance_id,
            "uid": source["user_id"],
            "folder_id": folder_id,
            "raw_asset_id": raw_asset_id,
            "connector_id": connector_id,
            "display_name": item.get("title") or item.get("origin_ref"),
            "origin_ref": item.get("origin_ref"),
            "origin_ref_type": item.get("origin_ref_type"),
            "doc_kind": item.get("doc_kind") or source.get("default_doc_kind"),
            "status": item.get("status") or "pending",
        },
    )
    updated = await database.database.fetch_one(
        """
        UPDATE source_items
        SET document_instance_id = :document_instance_id,
            updated_at = NOW()
        WHERE id = :id
          AND document_instance_id IS NULL
        RETURNING *
        """,
        {"id": item["id"], "document_instance_id": document_instance_id},
    )
    if updated:
        return dict(updated)
    item["document_instance_id"] = document_instance_id
    return item


async def _create_source_item(source_row, item: SourceItemCreate) -> dict[str, Any]:
    if not item.origin_ref:
        raise HTTPException(400, "origin_ref 不能为空")
    item_id = f"si_{secrets.token_hex(8)}"
    row = await database.database.fetch_one(
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
        ON CONFLICT (user_id, source_id, origin_ref_type, origin_ref)
        DO UPDATE SET
          raw_snapshot_ref = COALESCE(EXCLUDED.raw_snapshot_ref, source_items.raw_snapshot_ref),
          extracted_text_ref = COALESCE(EXCLUDED.extracted_text_ref, source_items.extracted_text_ref),
          content_hash = COALESCE(EXCLUDED.content_hash, source_items.content_hash),
          title = COALESCE(EXCLUDED.title, source_items.title),
          source_published_at = COALESCE(EXCLUDED.source_published_at, source_items.source_published_at),
          source_updated_at = COALESCE(EXCLUDED.source_updated_at, source_items.source_updated_at),
          captured_at = COALESCE(EXCLUDED.captured_at, source_items.captured_at),
          effective_at = COALESCE(EXCLUDED.effective_at, source_items.effective_at),
          doc_kind = COALESCE(EXCLUDED.doc_kind, source_items.doc_kind),
          raw_retention_policy = COALESCE(EXCLUDED.raw_retention_policy, source_items.raw_retention_policy),
          status = CASE
            WHEN source_items.status = 'succeeded' THEN source_items.status
            ELSE EXCLUDED.status
          END,
          error = NULL,
          updated_at = NOW()
        RETURNING *
        """,
        {
            "id": item_id,
            "user_id": source_row["user_id"],
            "source_id": source_row["id"],
            "source_type": source_row["type"],
            "origin_ref": item.origin_ref,
            "origin_ref_type": item.origin_ref_type,
            "raw_snapshot_ref": item.raw_snapshot_ref,
            "extracted_text_ref": item.extracted_text_ref,
            "content_hash": item.content_hash,
            "title": item.title,
            "source_published_at": item.source_published_at,
            "source_updated_at": item.source_updated_at,
            "captured_at": item.captured_at,
            "effective_at": item.effective_at,
            "doc_kind": _validate_doc_kind(item.doc_kind),
            "raw_retention_policy": item.raw_retention_policy,
            "status": item.status,
        },
    )
    row = await _ensure_document_instance_for_source_item(source_row, row)
    return _serialize_source_item(row)


def _source_config(row) -> dict[str, Any]:
    cfg = row["config"] if row and row["config"] else {}
    if isinstance(cfg, str):
        try:
            return json.loads(cfg)
        except json.JSONDecodeError:
            return {}
    return dict(cfg)


def _feed_id_from_link(link: str) -> str | None:
    path = urlparse(link).path
    marker = "/feed/"
    if marker not in path:
        return None
    feed_part = path.split(marker, 1)[1].rsplit("/", 1)[-1]
    return feed_part.rsplit(".", 1)[0] or None


async def _fetch_wechat2rss_list() -> list[dict[str, Any]]:
    if not WECHAT2RSS_TOKEN:
        raise HTTPException(503, "WECHAT2RSS_TOKEN 未配置")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WECHAT2RSS_BASE_URL.rstrip('/')}/list",
                params={"k": WECHAT2RSS_TOKEN, "page": 1, "size": 500},
                timeout=10,
            )
            resp.raise_for_status()
    except httpx.RequestError as e:
        raise HTTPException(502, f"无法连接 wechat2rss: {e}")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"wechat2rss 返回错误: {e.response.status_code}")

    payload = resp.json()
    if payload.get("err"):
        raise HTTPException(502, f"wechat2rss 返回错误: {payload['err']}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise HTTPException(502, "wechat2rss /list 响应格式异常")
    return data


async def _wechat2rss_source_map() -> dict[str, dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        SELECT id, name, config, is_primary
        FROM sources
        WHERE type = 'wechat'
          AND config->>'provider' = 'wechat2rss'
          AND deleted_at IS NULL
        """
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        cfg = _source_config(row)
        feed_id = str(cfg.get("feed_id") or "")
        if feed_id:
            result[feed_id] = {
                "source_id": row["id"],
                "source_name": row["name"],
                "is_primary": row["is_primary"],
            }
    return result


@router.get("/wechat2rss/subscriptions")
async def list_wechat2rss_subscriptions(_: dict = Depends(require_auth)):
    source_map = await _wechat2rss_source_map()
    subscriptions: list[dict[str, Any]] = []
    for item in await _fetch_wechat2rss_list():
        raw_id = item.get("id")
        feed_id = str(raw_id) if raw_id is not None else _feed_id_from_link(str(item.get("link") or ""))
        if not feed_id:
            continue
        source = source_map.get(feed_id)
        subscriptions.append(
            {
                "feed_id": feed_id,
                "name": item.get("name") or feed_id,
                "enabled": bool(source),
                "source_id": source["source_id"] if source else None,
                "source_name": source["source_name"] if source else None,
                "is_primary": source["is_primary"] if source else None,
            }
        )
    return {"subscriptions": subscriptions}


@router.post("/wechat2rss/sources", status_code=status.HTTP_201_CREATED)
async def create_wechat2rss_source(body: Wechat2RSSSourceCreate, _: dict = Depends(require_auth)):
    feed_id = body.feed_id.strip()
    if not feed_id:
        raise HTTPException(400, "feed_id 不能为空")

    existing = await database.database.fetch_one(
        """
        SELECT * FROM sources
        WHERE type = 'wechat'
          AND config->>'provider' = 'wechat2rss'
          AND config->>'feed_id' = :feed_id
          AND deleted_at IS NULL
        """,
        {"feed_id": feed_id},
    )
    if existing:
        d = dict(existing)
        d["article_count"] = int(
            await database.database.fetch_val(
                "SELECT COUNT(*) FROM knowledge_nodes WHERE source_id = :id AND user_id = 'default'",
                {"id": d["id"]},
            )
            or 0
        )
        if d.get("last_fetched_at"):
            d["last_fetched_at"] = d["last_fetched_at"].isoformat()
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        return d

    name = (body.name or "").strip()
    if not name:
        for item in await _fetch_wechat2rss_list():
            candidate = str(item.get("id")) if item.get("id") is not None else _feed_id_from_link(str(item.get("link") or ""))
            if candidate == feed_id:
                name = str(item.get("name") or feed_id)
                break
    if not name:
        name = feed_id

    source_id = f"src_{secrets.token_hex(6)}"
    await database.database.execute(
        """
        INSERT INTO sources (id, user_id, name, type, fetch_mode, is_primary, config, api_token)
        VALUES (:id, :user_id, :name, 'wechat', 'subscription', :is_primary, :config, NULL)
        """,
        {
            "id": source_id,
            "user_id": USER_ID,
            "name": name,
            "is_primary": body.is_primary,
            "config": database.jsonb(
                {
                    "provider": "wechat2rss",
                    "feed_id": feed_id,
                    "name": name,
                }
            ),
        },
    )
    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    assert row is not None
    d: dict[str, Any] = dict(row)  # type: ignore[arg-type]
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    d["article_count"] = 0
    return d


@router.get("/{source_id}/source-items")
async def list_source_items(
    source_id: str,
    status: str | None = None,
    limit: int = 100,
    _: dict = Depends(require_auth_or_service_token),
):
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id AND deleted_at IS NULL",
        {"id": source_id},
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    limit = max(1, min(limit, 500))
    params: dict[str, Any] = {"source_id": source_id}
    where = "source_id = :source_id"
    if status:
        where += " AND status = :status"
        params["status"] = status
    rows = await database.database.fetch_all(
        f"""
        SELECT * FROM source_items
        WHERE {where}
        ORDER BY created_at ASC
        LIMIT {limit}
        """,
        params,
    )
    return [_serialize_source_item(r) for r in rows]


@router.post("/{source_id}/source-items", status_code=status.HTTP_201_CREATED)
async def create_source_items(
    source_id: str,
    body: SourceItemsCreate,
    _: dict = Depends(require_auth_or_service_token),
):
    row = await database.database.fetch_one(
        """
        SELECT id, user_id, type, fetch_mode, default_doc_kind
        FROM sources
        WHERE id = :id AND deleted_at IS NULL
        """,
        {"id": source_id},
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    if not body.items:
        raise HTTPException(400, "items 不能为空")
    created = [await _create_source_item(row, item) for item in body.items]
    return {"ok": True, "items": created}


@router.post("/source-items/{item_id}/status")
async def update_source_item_status(
    item_id: str,
    body: SourceItemStatusUpdate,
    _: dict = Depends(require_auth_or_service_token),
):
    if body.status not in {"pending", "processing", "succeeded", "failed", "ignored"}:
        raise HTTPException(400, "不支持的 source item 状态")

    updates = ["status = :status", "updated_at = NOW()"]
    params: dict[str, Any] = {
        "id": item_id,
        "status": body.status,
    }
    if body.status == "processing":
        updates.append("attempts = attempts + 1")
        updates.append("error = NULL")
    elif body.status == "failed":
        updates.append("error = :error")
        params["error"] = body.error
    elif body.status == "succeeded":
        updates.append("error = NULL")
    if body.raw_snapshot_ref is not None:
        updates.append("raw_snapshot_ref = :raw_snapshot_ref")
        params["raw_snapshot_ref"] = body.raw_snapshot_ref
    if body.extracted_text_ref is not None:
        updates.append("extracted_text_ref = :extracted_text_ref")
        params["extracted_text_ref"] = body.extracted_text_ref
    if body.title is not None:
        updates.append("title = :title")
        params["title"] = body.title

    row = await database.database.fetch_one(
        f"""
        UPDATE source_items
        SET {', '.join(updates)}
        WHERE id = :id
        RETURNING *
        """,
        params,
    )
    if not row:
        raise HTTPException(404, "source item 不存在")
    if row["document_instance_id"]:
        await database.database.execute(
            """
            UPDATE document_instances
            SET status = :status,
                display_name = COALESCE(:title, display_name),
                updated_at = NOW()
            WHERE id = :id
            """,
            {"id": row["document_instance_id"], "status": body.status, "title": body.title},
        )
    return _serialize_source_item(row)


@router.post("/source-items/{item_id}/retry")
async def retry_source_item(item_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        """
        UPDATE source_items
        SET status = 'pending', error = NULL, updated_at = NOW()
        WHERE id = :id AND status = 'failed'
        RETURNING *
        """,
        {"id": item_id},
    )
    if not row:
        raise HTTPException(404, "failed source item 不存在")
    if row["document_instance_id"]:
        await database.database.execute(
            "UPDATE document_instances SET status = 'pending', updated_at = NOW() WHERE id = :id",
            {"id": row["document_instance_id"]},
        )
    return _serialize_source_item(row)


@router.patch("/source-items/{item_id}")
async def update_source_item(item_id: str, body: SourceItemUpdate, _: dict = Depends(require_auth)):
    doc_kind = _validate_doc_kind(body.doc_kind)
    row = await database.database.fetch_one(
        """
        UPDATE source_items
        SET doc_kind = :doc_kind,
            updated_at = NOW()
        WHERE id = :id
        RETURNING *
        """,
        {"id": item_id, "doc_kind": doc_kind},
    )
    if not row:
        raise HTTPException(404, "source item 不存在")
    if row["document_instance_id"]:
        await database.database.execute(
            "UPDATE document_instances SET doc_kind = :doc_kind, updated_at = NOW() WHERE id = :id",
            {"id": row["document_instance_id"], "doc_kind": doc_kind},
        )

    # 如果该 source item 已经入库为 article node，同步 node.doc_kind，避免 item/node 显示不一致。
    await database.database.execute(
        """
        UPDATE knowledge_nodes n
        SET doc_kind = :doc_kind,
            updated_at = NOW()
        FROM article_nodes an
        WHERE an.node_id = n.id
          AND an.source_item_id = :id
        """,
        {"id": item_id, "doc_kind": doc_kind},
    )
    return _serialize_source_item(row)


@router.get("/pending/source-ids")
async def list_pending_source_ids(_: dict = Depends(require_auth_or_service_token)):
    """Source ids that still have pending items. The ingestion worker polls this
    so manual uploads whose trigger ping was missed still get processed."""
    rows = await database.database.fetch_all(
        """
        SELECT DISTINCT si.source_id
        FROM source_items si
        JOIN sources s ON s.id = si.source_id
        WHERE si.status = 'pending'
          AND si.source_id IS NOT NULL
          AND s.deleted_at IS NULL
        """
    )
    return {"source_ids": [r["source_id"] for r in rows]}


@router.get("/{source_id}")
async def get_source(source_id: str, _: dict = Depends(require_auth_or_service_token)):
    """获取单个 source 详情（含文章数）。"""
    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    count = await database.database.fetch_val(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE source_id = :id AND user_id = 'default'",
        {"id": source_id},
    )
    d = dict(row)
    d["article_count"] = int(count or 0)
    if d.get("last_fetched_at"):
        d["last_fetched_at"] = d["last_fetched_at"].isoformat()
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("deleted_at"):
        d["deleted_at"] = d["deleted_at"].isoformat()
    return d


@router.get("")
async def list_sources(
    include_deleted: bool = False,
    _: dict = Depends(require_auth_or_service_token),
):
    """列出所有 sources（附带每个 source 的文章数）。"""
    deleted_filter = "" if include_deleted else "WHERE deleted_at IS NULL"
    rows = await database.database.fetch_all(
        f"SELECT * FROM sources {deleted_filter} ORDER BY deleted_at NULLS FIRST, created_at DESC"
    )
    counts = await database.database.fetch_all(
        "SELECT source_id, COUNT(*) AS cnt FROM knowledge_nodes"
        " WHERE user_id = 'default' GROUP BY source_id"
    )
    count_map = {r["source_id"]: int(r["cnt"]) for r in counts}
    result = []
    for r in rows:
        d = dict(r)
        d["article_count"] = count_map.get(d["id"], 0)
        if d.get("last_fetched_at"):
            d["last_fetched_at"] = d["last_fetched_at"].isoformat()
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        if d.get("deleted_at"):
            d["deleted_at"] = d["deleted_at"].isoformat()
        result.append(d)
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, _: dict = Depends(require_auth)):
    if body.type not in FETCH_MODES:
        raise HTTPException(400, f"不支持的 source 类型: {body.type}")
    if body.type == "wechat":
        raise HTTPException(400, "微信公众号 source 请通过 wechat2rss 订阅列表创建")

    source_id = f"src_{secrets.token_hex(6)}"

    await database.database.execute(
        """
        INSERT INTO sources (id, user_id, name, type, fetch_mode, is_primary,
                             config, api_token, default_doc_kind)
        VALUES (:id, :user_id, :name, :type, :fetch_mode, :is_primary,
                :config, :api_token, :default_doc_kind)
        """,
        {
            "id": source_id,
            "user_id": USER_ID,
            "name": body.name,
            "type": body.type,
            "fetch_mode": FETCH_MODES[body.type],
            "is_primary": body.is_primary,
            "config": database.jsonb(body.config),
            "api_token": None,
            "default_doc_kind": _validate_doc_kind(body.default_doc_kind),
        },
    )
    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    assert row is not None
    d: dict[str, Any] = dict(row)  # type: ignore[arg-type]
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    d["article_count"] = 0
    return d


@router.post("/{source_id}/upload")
async def upload_to_source(
    source_id: str,
    files: list[UploadFile] = File(...),
    captured_at: str | None = Form(None),
    effective_at: str | None = Form(None),
    doc_kind: str | None = Form(None),
    _: dict = Depends(require_auth),
):
    """向已有 source 上传一批文件（支持多文件），存储并触发 ingestion-worker 处理。
    Source 是持久渠道，可随时追加内容。"""
    row = await database.database.fetch_one(
        "SELECT id, user_id, type FROM sources WHERE id = :id AND deleted_at IS NULL",
        {"id": source_id},
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    src_type = row["type"]
    if src_type not in FILE_ACCEPT:
        raise HTTPException(400, f"source 类型 {src_type} 不支持文件上传")

    raw_dir = USER_DATA_DIR / USER_ID / "raw" / src_type
    raw_dir.mkdir(parents=True, exist_ok=True)

    captured_at = _validate_optional_time(captured_at, "captured_at")
    effective_at = _validate_optional_time(effective_at, "effective_at")
    doc_kind = _validate_doc_kind(doc_kind)
    now = datetime.now(timezone.utc)

    saved: list[str] = []
    source_items: list[dict[str, Any]] = []
    for file in files:
        safe_name = f"{date.today()}-{secrets.token_hex(4)}-{file.filename or 'upload'}"
        file_path = raw_dir / safe_name
        content = await file.read()
        file_path.write_bytes(content)
        saved.append(str(file_path))
        item = await _create_source_item(
            row,
            SourceItemCreate(
                origin_ref=f"upload://{safe_name}",
                origin_ref_type="upload",
                raw_snapshot_ref=str(file_path),
                content_hash=hashlib.sha256(content).hexdigest(),
                title=Path(file.filename or safe_name).stem,
                captured_at=datetime.fromisoformat(captured_at.replace("Z", "+00:00")) if captured_at else now,
                effective_at=datetime.fromisoformat(effective_at.replace("Z", "+00:00")) if effective_at else now,
                doc_kind=doc_kind,
                raw_retention_policy="keep_raw",
            ),
        )
        source_items.append(item)

    # 触发 ingestion-worker（best-effort；失败由 worker 轮询兜底）
    triggered = await _trigger_ingestion(source_id)

    return {
        "ok": True,
        "files_saved": len(saved),
        "source_items": source_items,
        "triggered": triggered,
    }


@router.post("/{source_id}/add-url")
async def add_url_to_source(
    source_id: str,
    body: dict,
    _: dict = Depends(require_auth),
):
    """向已有 URL source 追加一条或多条 URL，触发 ingestion-worker 处理。"""
    row = await database.database.fetch_one(
        "SELECT id, user_id, type FROM sources WHERE id = :id AND deleted_at IS NULL",
        {"id": source_id},
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    if row["type"] != "url":
        raise HTTPException(400, "仅 url 类型 source 支持此操作")

    urls: list[str] = body.get("urls", [])
    if not urls:
        raise HTTPException(400, "至少提供一个 URL")

    doc_kind = _validate_doc_kind(body.get("doc_kind"))

    source_items = [
        await _create_source_item(
            row,
            SourceItemCreate(
                origin_ref=url,
                origin_ref_type="url",
                content_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                captured_at=datetime.now(timezone.utc),
                doc_kind=doc_kind,
                raw_retention_policy="keep_extracted_only",
            ),
        )
        for url in urls
    ]

    triggered = await _trigger_ingestion(source_id)

    return {
        "ok": True,
        "urls_queued": len(urls),
        "source_items": source_items,
        "triggered": triggered,
    }


@router.post("/{source_id}/fetch")
async def trigger_fetch(source_id: str, _: dict = Depends(require_auth)):
    """触发 ingestion-worker 立即抓取指定 source。"""
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"无法连接 ingestion-worker: {e}")
    return {"ok": True}


@router.put("/{source_id}")
async def update_source(
    source_id: str,
    body: SourceUpdate,
    _: dict = Depends(require_auth_or_service_token),
):
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")

    updates: list[str] = []
    params: dict = {"id": source_id}

    if body.name is not None:
        updates.append("name = :name")
        params["name"] = body.name
    if body.config is not None:
        updates.append("config = :config")
        params["config"] = database.jsonb(body.config)
    if body.is_primary is not None:
        updates.append("is_primary = :is_primary")
        params["is_primary"] = body.is_primary
    if body.default_doc_kind is not None:
        updates.append("default_doc_kind = :default_doc_kind")
        params["default_doc_kind"] = _validate_doc_kind(body.default_doc_kind) or ""
    if body.last_fetched_at is not None:
        updates.append("last_fetched_at = :last_fetched_at")
        params["last_fetched_at"] = datetime.fromisoformat(body.last_fetched_at)

    if updates:
        await database.database.execute(
            f"UPDATE sources SET {', '.join(updates)} WHERE id = :id", params
        )
        if body.last_fetched_at is not None:
            await database.database.execute(
                "UPDATE connectors SET last_fetched_at = :ts, updated_at = NOW() WHERE id = :con_id",
                {"ts": datetime.fromisoformat(body.last_fetched_at),
                 "con_id": "con_" + source_id[4:]},
            )

    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    assert row is not None
    d: dict[str, Any] = dict(row)  # type: ignore[arg-type]
    if d.get("last_fetched_at"):
        d["last_fetched_at"] = d["last_fetched_at"].isoformat()
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("deleted_at"):
        d["deleted_at"] = d["deleted_at"].isoformat()
    return d


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id AND deleted_at IS NULL", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    await database.database.execute(
        "UPDATE sources SET deleted_at = NOW() WHERE id = :id", {"id": source_id}
    )

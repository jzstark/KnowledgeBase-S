"""
资料夹 / 文档实例 / Connector 管理 API。

架构分层：
  folders         — 用户组织层（normal | stream）
  connectors      — stream 资料夹的外部接入（rss | wechat）
  raw_assets      — 物理存储引用
  document_instances — 资料夹条目，指向 raw_asset

每个 folder 在创建时自动生成一条对应的 legacy source（维持 ingestion-worker 向后兼容）。
ID 映射约定：
  fld_{hex} <-> src_{hex}  (同 hex 后缀)
  con_{hex} <-> src_{hex}  (stream source)
  di_{hex}  <-> si_{hex}   (同 hex 后缀，前缀不等长)
  ra_{hex}  <-> si_{hex}
"""

import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from pydantic import BaseModel

import database
from auth import require_auth
from document_types import (
    DocumentTypeError,
    set_document_instance_doc_kind,
    validate_explicit_doc_kind,
)
from settings import settings
from kb.internal import do_delete_node
from kb.summary import do_delete_summary

router = APIRouter(prefix="/api/folders", tags=["folders"])
di_router = APIRouter(prefix="/api/document-instances", tags=["document-instances"])
connector_router = APIRouter(prefix="/api/connectors", tags=["connectors"])

INGESTION_WORKER_URL = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")
WECHAT2RSS_BASE_URL = os.environ.get("WECHAT2RSS_BASE_URL", "https://rss.laughtale.co.uk/wechat-api")
WECHAT2RSS_FEED_BASE_URL = os.environ.get("WECHAT2RSS_FEED_BASE_URL", "https://rss.laughtale.co.uk/wechat")
WECHAT2RSS_TOKEN = os.environ.get("WECHAT2RSS_TOKEN", "")
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"
logger = logging.getLogger(__name__)

FILE_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".txt": "text/plain", ".md": "text/markdown",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
}
FILE_SOURCE_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image", ".webp": "image",
    ".txt": "plaintext", ".md": "plaintext",
    ".doc": "word", ".docx": "word",
    ".epub": "epub", ".mobi": "epub",
}


# ── ID 映射工具 ───────────────────────────────────────────────────────────────

def _source_id(folder_id: str) -> str:
    return "src_" + folder_id[4:]   # fld_XXXX → src_XXXX


def _di_id(si_id: str) -> str:
    return "di_" + si_id[3:]        # si_XXXX → di_XXXX


def _ra_id(si_id: str) -> str:
    return "ra_" + si_id[3:]        # si_XXXX → ra_XXXX


def _con_id(source_id: str) -> str:
    return "con_" + source_id[4:]   # src_XXXX → con_XXXX


def _validate_doc_kind(val: str | None) -> str | None:
    if not val:
        return None
    allowed = set(settings.doc_kind.values)
    if allowed and val not in allowed:
        return settings.doc_kind.default
    return val


def _serialize_timestamps(d: dict, *keys: str) -> dict:
    for k in keys:
        v = d.get(k)
        if v and not isinstance(v, str):
            d[k] = v.isoformat()
    return d


# ── Folder CRUD ───────────────────────────────────────────────────────────────

class FolderCreate(BaseModel):
    name: str
    parent_id: str | None = None
    kind: str = "normal"           # normal | stream
    default_doc_kind: str | None = None


class FolderUpdate(BaseModel):
    name: str | None = None
    parent_id: str | None = None   # None means "no change"; use empty string to move to root
    status: str | None = None      # archived


@router.get("")
async def list_folders(_: dict = Depends(require_auth)) -> list[dict]:
    rows = await database.database.fetch_all(
        "SELECT * FROM folders WHERE user_id = :uid ORDER BY kind DESC, name",
        {"uid": USER_ID},
    )
    result = []
    for row in rows:
        d = _serialize_timestamps(dict(row), "created_at", "updated_at")
        # 附加 document_instance 计数
        cnt = await database.database.fetch_val(
            """
            SELECT COUNT(*) FROM document_instances
            WHERE folder_id = :fid AND status NOT IN ('ignored', 'deleted')
            """,
            {"fid": row["id"]},
        )
        d["item_count"] = int(cnt or 0)
        result.append(d)
    return result


@router.post("", status_code=201)
async def create_folder(body: FolderCreate, _: dict = Depends(require_auth)) -> dict:
    hex_suffix = secrets.token_hex(8)
    folder_id = f"fld_{hex_suffix}"
    source_id = f"src_{hex_suffix}"
    kind = body.kind if body.kind in ("normal", "stream") else "normal"

    # 创建 folder
    await database.database.execute(
        """
        INSERT INTO folders (id, user_id, parent_id, name, kind, status, created_at, updated_at)
        VALUES (:id, :uid, :parent_id, :name, :kind, 'active', NOW(), NOW())
        """,
        {"id": folder_id, "uid": USER_ID, "parent_id": body.parent_id,
         "name": body.name.strip(), "kind": kind},
    )

    # 创建对应 legacy source（ingestion-worker 向后兼容）
    source_type = "rss" if kind == "stream" else "url"
    await database.database.execute(
        """
        INSERT INTO sources (id, user_id, name, type, fetch_mode, is_primary, default_doc_kind,
                             config, created_at)
        VALUES (:id, :uid, :name, :type, :fetch_mode, false, :doc_kind, :config, NOW())
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": source_id,
            "uid": USER_ID,
            "name": body.name.strip(),
            "type": source_type,
            "fetch_mode": "subscription" if kind == "stream" else "manual",
            "doc_kind": _validate_doc_kind(body.default_doc_kind),
            "config": database.jsonb({}),
        },
    )

    row = await database.database.fetch_one(
        "SELECT * FROM folders WHERE id = :id", {"id": folder_id}
    )
    assert row is not None
    d = _serialize_timestamps(dict(row), "created_at", "updated_at")
    d["item_count"] = 0
    return d


@router.get("/{folder_id}")
async def get_folder(folder_id: str, _: dict = Depends(require_auth)) -> dict:
    row = await database.database.fetch_one(
        "SELECT * FROM folders WHERE id = :id AND user_id = :uid",
        {"id": folder_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "资料夹不存在")
    d = _serialize_timestamps(dict(row), "created_at", "updated_at")
    cnt = await database.database.fetch_val(
        "SELECT COUNT(*) FROM document_instances WHERE folder_id = :fid", {"fid": folder_id}
    )
    d["item_count"] = int(cnt or 0)
    return d


@router.patch("/{folder_id}")
async def update_folder(folder_id: str, body: FolderUpdate, _: dict = Depends(require_auth)) -> dict:
    row = await database.database.fetch_one(
        "SELECT * FROM folders WHERE id = :id AND user_id = :uid",
        {"id": folder_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "资料夹不存在")

    updates = ["updated_at = NOW()"]
    params: dict[str, Any] = {"id": folder_id}

    if body.name is not None:
        updates.append("name = :name")
        params["name"] = body.name.strip()
        # 同步 legacy source 名称
        await database.database.execute(
            "UPDATE sources SET name = :name WHERE id = :sid",
            {"name": body.name.strip(), "sid": _source_id(folder_id)},
        )
    if body.status in ("active", "archived"):
        updates.append("status = :status")
        params["status"] = body.status
    if body.parent_id is not None:
        updates.append("parent_id = :parent_id")
        params["parent_id"] = body.parent_id or None

    await database.database.execute(
        f"UPDATE folders SET {', '.join(updates)} WHERE id = :id", params
    )
    return await get_folder(folder_id, _)


@router.delete("/{folder_id}", status_code=204)
async def delete_folder(folder_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :id AND user_id = :uid",
        {"id": folder_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "资料夹不存在")
    cnt = await database.database.fetch_val(
        "SELECT COUNT(*) FROM document_instances WHERE folder_id = :fid", {"fid": folder_id}
    )
    if int(cnt or 0) > 0:
        raise HTTPException(400, f"资料夹非空（{cnt} 条文档），请先移除内容再删除")
    await database.database.execute(
        "UPDATE folders SET status = 'archived', updated_at = NOW() WHERE id = :id",
        {"id": folder_id},
    )
    await database.database.execute(
        "UPDATE sources SET deleted_at = NOW() WHERE id = :sid",
        {"sid": _source_id(folder_id)},
    )


# ── Folder Contents ───────────────────────────────────────────────────────────

@router.get("/{folder_id}/contents")
async def get_folder_contents(
    folder_id: str,
    status: str | None = None,
    include_archived: bool = False,
    _: dict = Depends(require_auth),
) -> dict:
    folder = await database.database.fetch_one(
        "SELECT * FROM folders WHERE id = :id AND user_id = :uid",
        {"id": folder_id, "uid": USER_ID},
    )
    if not folder:
        raise HTTPException(404, "资料夹不存在")

    # 子资料夹
    sub_rows = await database.database.fetch_all(
        "SELECT * FROM folders WHERE parent_id = :fid AND user_id = :uid ORDER BY name",
        {"fid": folder_id, "uid": USER_ID},
    )
    subfolders = [_serialize_timestamps(dict(r), "created_at", "updated_at") for r in sub_rows]

    # 文档实例
    q = """
        SELECT di.*,
               ra.mime_type,
               ra.size,
               articles.article_id,
               articles.article_title,
               articles.article_titles,
               articles.article_doc_kind
        FROM document_instances di
        LEFT JOIN raw_assets ra ON ra.id = di.raw_asset_id
        LEFT JOIN LATERAL (
            SELECT
                (array_agg(an.node_id ORDER BY kn.created_at DESC NULLS LAST, an.node_id))[1]
                    AS article_id,
                (array_agg(kn.title ORDER BY kn.created_at DESC NULLS LAST, an.node_id))[1]
                    AS article_title,
                (array_agg(kn.doc_kind ORDER BY kn.created_at DESC NULLS LAST, an.node_id))[1]
                    AS article_doc_kind,
                COALESCE(
                    array_agg(kn.title ORDER BY kn.created_at DESC NULLS LAST, an.node_id)
                        FILTER (WHERE kn.title IS NOT NULL),
                    ARRAY[]::text[]
                ) AS article_titles
            FROM article_nodes an
            JOIN knowledge_nodes kn ON kn.id = an.node_id
            WHERE an.document_instance_id = di.id
        ) articles ON TRUE
        WHERE di.folder_id = :fid
    """
    params: dict[str, Any] = {"fid": folder_id}
    if status:
        q += " AND di.status = :status"
        params["status"] = status
    elif include_archived:
        q += " AND di.status <> 'deleted'"
    else:
        q += " AND di.status NOT IN ('ignored', 'deleted')"
    q += " ORDER BY di.created_at DESC"
    di_rows = await database.database.fetch_all(q, params)
    items = [_serialize_timestamps(dict(r), "created_at", "updated_at") for r in di_rows]

    # connector（stream 资料夹）
    connector = None
    if folder["kind"] == "stream":
        con_row = await database.database.fetch_one(
            "SELECT * FROM connectors WHERE folder_id = :fid", {"fid": folder_id}
        )
        if con_row:
            connector = _serialize_timestamps(dict(con_row), "last_fetched_at", "created_at", "updated_at")

    return {
        "folder": _serialize_timestamps(dict(folder), "created_at", "updated_at"),
        "subfolders": subfolders,
        "items": items,
        "connector": connector,
    }


# ── Folder Upload / Add-URL ───────────────────────────────────────────────────

@router.post("/{folder_id}/upload")
async def upload_to_folder(
    folder_id: str,
    files: list[UploadFile] = File(...),
    captured_at: str | None = Form(None),
    effective_at: str | None = Form(None),
    doc_kind: str | None = Form(None),
    _: dict = Depends(require_auth),
):
    folder = await database.database.fetch_one(
        "SELECT * FROM folders WHERE id = :id AND user_id = :uid AND status = 'active'",
        {"id": folder_id, "uid": USER_ID},
    )
    if not folder:
        raise HTTPException(404, "资料夹不存在或已归档")

    source_id = _source_id(folder_id)
    source_row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id", {"id": source_id}
    )
    if not source_row:
        raise HTTPException(500, "资料夹缺少 legacy source，请重建资料夹")

    doc_kind_val = _validate_doc_kind(doc_kind)
    now = datetime.now(timezone.utc)
    cap_dt = _parse_dt(captured_at) or now
    eff_dt = _parse_dt(effective_at) or now

    created_items = []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        src_type = FILE_SOURCE_TYPE.get(ext, "plaintext")
        mime = FILE_MIME.get(ext, "application/octet-stream")

        # 存文件
        raw_dir = USER_DATA_DIR / USER_ID / "raw" / src_type
        raw_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(4)}-{file.filename or 'upload'}"
        file_path = raw_dir / safe_name
        content = await file.read()
        file_path.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        display_name = Path(file.filename or safe_name).stem

        si_hex = secrets.token_hex(8)
        si_id = f"si_{si_hex}"
        ra_id = f"ra_{si_hex}"
        di_id = f"di_{si_hex}"

        # raw_asset
        await database.database.execute(
            """
            INSERT INTO raw_assets (id, user_id, storage_key, original_filename, mime_type, size, sha256, created_at)
            VALUES (:id, :uid, :storage_key, :fname, :mime, :size, :sha256, NOW())
            """,
            {"id": ra_id, "uid": USER_ID, "storage_key": str(file_path),
             "fname": file.filename, "mime": mime, "size": len(content), "sha256": sha256},
        )

        # document_instance
        await database.database.execute(
            """
            INSERT INTO document_instances
              (id, user_id, folder_id, raw_asset_id, display_name, origin_ref, origin_ref_type,
               doc_kind, status, created_at, updated_at)
            VALUES (:id, :uid, :fid, :ra_id, :name, :origin_ref, 'upload', :doc_kind, 'pending', NOW(), NOW())
            """,
            {"id": di_id, "uid": USER_ID, "fid": folder_id, "ra_id": ra_id,
             "name": display_name, "origin_ref": f"upload://{safe_name}",
             "doc_kind": doc_kind_val},
        )

        # source_item（ingestion-worker 向后兼容）
        si_row = await database.database.fetch_one(
            """
            INSERT INTO source_items
              (id, user_id, source_id, source_type, origin_ref, origin_ref_type,
               raw_snapshot_ref, content_hash, title, captured_at, effective_at,
               doc_kind, raw_retention_policy, document_instance_id, status)
            VALUES
              (:id, :uid, :source_id, :src_type, :origin_ref, 'upload',
               :raw_snapshot_ref, :hash, :title, :cap, :eff,
               :doc_kind, 'keep_raw', :di_id, 'pending')
            ON CONFLICT (user_id, source_id, origin_ref_type, origin_ref)
            DO UPDATE SET
              raw_snapshot_ref = EXCLUDED.raw_snapshot_ref,
              document_instance_id = EXCLUDED.document_instance_id,
              status = CASE WHEN source_items.status = 'succeeded' THEN source_items.status ELSE 'pending' END,
              updated_at = NOW()
            RETURNING *
            """,
            {
                "id": si_id, "uid": USER_ID, "source_id": source_id, "src_type": src_type,
                "origin_ref": f"upload://{safe_name}", "raw_snapshot_ref": str(file_path),
                "hash": sha256, "title": display_name, "cap": cap_dt, "eff": eff_dt,
                "doc_kind": doc_kind_val, "di_id": di_id,
            },
        )
        assert si_row is not None
        created_items.append({"document_instance_id": di_id, "source_item_id": si_row["id"]})

    # 触发 ingestion-worker
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5)
    except Exception:
        pass

    return {"ok": True, "files_saved": len(files), "items": created_items}


@router.post("/{folder_id}/add-url")
async def add_url_to_folder(
    folder_id: str,
    body: dict,
    _: dict = Depends(require_auth),
):
    folder = await database.database.fetch_one(
        "SELECT * FROM folders WHERE id = :id AND user_id = :uid AND status = 'active'",
        {"id": folder_id, "uid": USER_ID},
    )
    if not folder:
        raise HTTPException(404, "资料夹不存在或已归档")

    source_id = _source_id(folder_id)
    source_row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id", {"id": source_id}
    )
    if not source_row:
        raise HTTPException(500, "资料夹缺少 legacy source，请重建资料夹")

    urls: list[str] = body.get("urls", [])
    if not urls:
        raise HTTPException(400, "至少提供一个 URL")
    doc_kind_val = _validate_doc_kind(body.get("doc_kind"))
    now = datetime.now(timezone.utc)

    created_items = []
    for url in urls:
        si_hex = secrets.token_hex(8)
        si_id = f"si_{si_hex}"
        ra_id = f"ra_{si_hex}"
        di_id = f"di_{si_hex}"
        url_hash = hashlib.sha256(url.encode()).hexdigest()

        await database.database.execute(
            """
            INSERT INTO raw_assets (id, user_id, storage_key, original_filename, mime_type, sha256, created_at)
            VALUES (:id, :uid, :url, :url, 'text/html', :sha256, NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            {"id": ra_id, "uid": USER_ID, "url": url, "sha256": url_hash},
        )
        await database.database.execute(
            """
            INSERT INTO document_instances
              (id, user_id, folder_id, raw_asset_id, display_name, origin_ref, origin_ref_type,
               doc_kind, status, created_at, updated_at)
            VALUES (:id, :uid, :fid, :ra_id, :url, :url, 'url', :doc_kind, 'pending', NOW(), NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            {"id": di_id, "uid": USER_ID, "fid": folder_id, "ra_id": ra_id,
             "url": url, "doc_kind": doc_kind_val},
        )
        si_row = await database.database.fetch_one(
            """
            INSERT INTO source_items
              (id, user_id, source_id, source_type, origin_ref, origin_ref_type,
               content_hash, captured_at, doc_kind, raw_retention_policy, document_instance_id, status)
            VALUES
              (:id, :uid, :source_id, 'url', :url, 'url',
               :hash, :now, :doc_kind, 'keep_extracted_only', :di_id, 'pending')
            ON CONFLICT (user_id, source_id, origin_ref_type, origin_ref)
            DO UPDATE SET
              document_instance_id = EXCLUDED.document_instance_id,
              status = CASE WHEN source_items.status = 'succeeded' THEN source_items.status ELSE 'pending' END,
              updated_at = NOW()
            RETURNING id
            """,
            {"id": si_id, "uid": USER_ID, "source_id": source_id, "url": url,
             "hash": url_hash, "now": now, "doc_kind": doc_kind_val, "di_id": di_id},
        )
        assert si_row is not None
        created_items.append({"document_instance_id": di_id, "source_item_id": si_row["id"]})

    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5)
    except Exception:
        pass

    return {"ok": True, "urls_queued": len(urls), "items": created_items}


# ── Document Instance CRUD ────────────────────────────────────────────────────

class DocumentInstanceUpdate(BaseModel):
    display_name: str | None = None
    folder_id: str | None = None
    doc_kind: str | None = None


class DocumentInstanceBatchRequest(BaseModel):
    folder_id: str
    ids: list[str]


class DocumentInstanceDocKindBatchRequest(DocumentInstanceBatchRequest):
    doc_kind: str


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


@di_router.post("/batch/archive")
async def archive_document_instances(
    body: DocumentInstanceBatchRequest,
    _: dict = Depends(require_auth),
) -> dict:
    ids = list(dict.fromkeys(body.ids))
    if not ids:
        raise HTTPException(400, "至少选择一篇文档")
    if len(ids) > 1000:
        raise HTTPException(400, "单次最多归档 1000 篇文档")

    folder = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :id AND user_id = :uid",
        {"id": body.folder_id, "uid": USER_ID},
    )
    if not folder:
        raise HTTPException(404, "资料夹不存在")

    results = []
    for di_id in ids:
        result, detail = await _archive_document_instance(di_id, folder_id=body.folder_id)
        results.append({"id": di_id, "status": result, "detail": detail})

    return {
        "ok": all(item["status"] != "failed" for item in results),
        "archived": sum(item["status"] == "archived" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "results": results,
    }


@di_router.patch("/batch/doc-kind")
async def update_document_instances_doc_kind(
    body: DocumentInstanceDocKindBatchRequest,
    _: dict = Depends(require_auth),
) -> dict:
    ids = list(dict.fromkeys(body.ids))
    if not ids:
        raise HTTPException(400, "至少选择一篇文档")
    if len(ids) > 1000:
        raise HTTPException(400, "单次最多修改 1000 篇文档")
    try:
        validate_explicit_doc_kind(body.doc_kind)
    except DocumentTypeError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    folder = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :id AND user_id = :uid",
        {"id": body.folder_id, "uid": USER_ID},
    )
    if not folder:
        raise HTTPException(404, "资料夹不存在")

    results = []
    for di_id in ids:
        try:
            result = await set_document_instance_doc_kind(
                di_id, body.doc_kind, user_id=USER_ID, folder_id=body.folder_id
            )
            results.append(
                {
                    "id": di_id,
                    "status": "updated",
                    "detail": "内容类型已更新",
                    "wiki_warnings": result.wiki_warnings,
                }
            )
        except DocumentTypeError as exc:
            results.append(
                {"id": di_id, "status": "failed", "detail": exc.detail, "wiki_warnings": []}
            )
        except Exception:
            logger.exception("failed to update doc_kind for document instance %s", di_id)
            results.append(
                {
                    "id": di_id,
                    "status": "failed",
                    "detail": "内容类型更新失败，请重试",
                    "wiki_warnings": [],
                }
            )

    warnings = [
        warning
        for item in results
        for warning in item["wiki_warnings"]
    ]
    return {
        "ok": all(item["status"] != "failed" for item in results),
        "updated": sum(item["status"] == "updated" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "wiki_warnings": warnings,
        "results": results,
    }


@di_router.get("/{di_id}")
async def get_document_instance(di_id: str, _: dict = Depends(require_auth)) -> dict:
    row = await database.database.fetch_one(
        """
        SELECT di.*,
               ra.storage_key, ra.original_filename, ra.mime_type, ra.size, ra.sha256,
               an.node_id as article_id,
               kn.title as article_title,
               kn.doc_kind as article_doc_kind
        FROM document_instances di
        LEFT JOIN raw_assets ra ON ra.id = di.raw_asset_id
        LEFT JOIN article_nodes an ON an.document_instance_id = di.id
        LEFT JOIN knowledge_nodes kn ON kn.id = an.node_id
        WHERE di.id = :id AND di.user_id = :uid
        """,
        {"id": di_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "文档实例不存在")
    return _serialize_timestamps(dict(row), "created_at", "updated_at")


@di_router.patch("/{di_id}")
async def update_document_instance(
    di_id: str,
    body: DocumentInstanceUpdate,
    _: dict = Depends(require_auth),
) -> dict:
    row = await database.database.fetch_one(
        "SELECT id FROM document_instances WHERE id = :id AND user_id = :uid",
        {"id": di_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "文档实例不存在")

    if body.doc_kind is not None:
        try:
            await set_document_instance_doc_kind(
                di_id, body.doc_kind, user_id=USER_ID
            )
        except DocumentTypeError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    updates = []
    params: dict[str, Any] = {"id": di_id}

    if body.display_name is not None:
        updates.append("display_name = :display_name")
        params["display_name"] = body.display_name.strip()
    if body.folder_id is not None:
        folder = await database.database.fetch_one(
            "SELECT id FROM folders WHERE id = :fid AND user_id = :uid",
            {"fid": body.folder_id, "uid": USER_ID},
        )
        if not folder:
            raise HTTPException(400, "目标资料夹不存在")
        updates.append("folder_id = :folder_id")
        params["folder_id"] = body.folder_id
    if updates:
        updates.append("updated_at = NOW()")
        await database.database.execute(
            f"UPDATE document_instances SET {', '.join(updates)} WHERE id = :id", params
        )
    return await get_document_instance(di_id, _)


@di_router.post("/{di_id}/copy")
async def copy_document_instance(
    di_id: str,
    body: dict,
    _: dict = Depends(require_auth),
) -> dict:
    row = await database.database.fetch_one(
        "SELECT * FROM document_instances WHERE id = :id AND user_id = :uid",
        {"id": di_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "文档实例不存在")
    target_folder_id = body.get("folder_id")
    if not target_folder_id:
        raise HTTPException(400, "必须指定目标资料夹 folder_id")
    target = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :fid AND user_id = :uid",
        {"fid": target_folder_id, "uid": USER_ID},
    )
    if not target:
        raise HTTPException(400, "目标资料夹不存在")

    new_di_id = f"di_{secrets.token_hex(8)}"
    await database.database.execute(
        """
        INSERT INTO document_instances
          (id, user_id, folder_id, raw_asset_id, connector_id, display_name,
           origin_ref, origin_ref_type, doc_kind, status, created_at, updated_at)
        VALUES
          (:id, :uid, :fid, :ra_id, :con_id, :name,
           :origin_ref, :origin_ref_type, :doc_kind, 'pending', NOW(), NOW())
        """,
        {
            "id": new_di_id, "uid": USER_ID, "fid": target_folder_id,
            "ra_id": row["raw_asset_id"], "con_id": row["connector_id"],
            "name": row["display_name"], "origin_ref": row["origin_ref"],
            "origin_ref_type": row["origin_ref_type"], "doc_kind": row["doc_kind"],
        },
    )
    return await get_document_instance(new_di_id, _)


async def _delete_di_files(di_id: str) -> None:
    """Unlink the raw-snapshot / extracted-text files for a document instance.
    Only removes files that resolve inside USER_DATA_DIR; URLs and missing refs
    are skipped."""
    rows = await database.database.fetch_all(
        """
        SELECT ra.storage_key, si.raw_snapshot_ref, si.extracted_text_ref
        FROM document_instances di
        LEFT JOIN raw_assets ra ON ra.id = di.raw_asset_id
        LEFT JOIN source_items si ON si.document_instance_id = di.id
        WHERE di.id = :id
        """,
        {"id": di_id},
    )
    base = USER_DATA_DIR.resolve()
    for r in rows:
        for ref in (r["storage_key"], r["raw_snapshot_ref"], r["extracted_text_ref"]):
            if not ref:
                continue
            try:
                p = Path(ref).resolve()
            except (OSError, ValueError):
                continue
            if p.is_file() and p.is_relative_to(base):
                p.unlink(missing_ok=True)


async def _hard_delete_article(node_id: str) -> None:
    """Delete an article node and everything hanging off it: all its summaries
    (their own knowledge_nodes + wiki files — the DB only cascades the
    summary_nodes row, not the summary node), then the article node, then prune
    the article id from the non-FK array columns."""
    summary_rows = await database.database.fetch_all(
        "SELECT node_id FROM summary_nodes WHERE summary_of = :id", {"id": node_id},
    )
    for s in summary_rows:
        try:
            await do_delete_summary(s["node_id"])
        except ValueError:
            pass  # already gone
    await do_delete_node(node_id)
    for table in ("entity_candidates", "entity_pair_signals"):
        await database.database.execute(
            f"UPDATE {table} SET source_article_ids = array_remove(source_article_ids, :id), "
            "updated_at = NOW() WHERE :id = ANY(source_article_ids)",
            {"id": node_id},
        )


async def _hard_delete_document_instance(di_id: str) -> None:
    """Permanently delete the article(s) behind a document instance plus their
    summaries and raw files, and tombstone the source item / document instance as
    'deleted' so a subscription feed re-listing the item won't resurrect it."""
    article_rows = await database.database.fetch_all(
        "SELECT node_id FROM article_nodes WHERE document_instance_id = :id", {"id": di_id},
    )
    for a in article_rows:
        await _hard_delete_article(a["node_id"])

    await _delete_di_files(di_id)

    await database.database.execute(
        "UPDATE source_items SET status = 'deleted', updated_at = NOW() WHERE document_instance_id = :id",
        {"id": di_id},
    )
    await database.database.execute(
        "UPDATE document_instances SET status = 'deleted', updated_at = NOW() WHERE id = :id",
        {"id": di_id},
    )


@di_router.delete("/{di_id}", status_code=204)
async def delete_document_instance(di_id: str, hard: bool = False, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT id FROM document_instances WHERE id = :id AND user_id = :uid",
        {"id": di_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "文档实例不存在")
    if hard:
        await _hard_delete_document_instance(di_id)
    else:
        result, detail = await _archive_document_instance(di_id)
        if result == "failed":
            raise HTTPException(409, detail)


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


@di_router.post("/batch/reprocess")
async def reprocess_document_instances(
    body: DocumentInstanceBatchRequest,
    _: dict = Depends(require_auth),
) -> dict:
    ids = list(dict.fromkeys(body.ids))
    if not ids:
        raise HTTPException(400, "至少选择一篇文档")
    if len(ids) > 1000:
        raise HTTPException(400, "单次最多重新处理 1000 篇文档")

    folder = await database.database.fetch_one(
        "SELECT id FROM folders WHERE id = :id AND user_id = :uid",
        {"id": body.folder_id, "uid": USER_ID},
    )
    if not folder:
        raise HTTPException(404, "资料夹不存在")

    results = []
    for di_id in ids:
        try:
            result = await _queue_document_instance_reprocess(
                di_id, folder_id=body.folder_id
            )
        except Exception:
            logger.exception("failed to queue document reprocess for %s", di_id)
            result = {
                "id": di_id,
                "status": "failed",
                "detail": "重新生成排队失败，请重试",
            }
        results.append(result)
    source_ids = {
        item["source_id"] for item in results if item["status"] == "accepted"
    }
    trigger_results = await _trigger_reprocess_sources(source_ids)
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
        "deferred_sources": sum(not value for value in trigger_results.values()),
        "results": results,
    }


@di_router.post("/{di_id}/reprocess")
async def reprocess_document_instance(di_id: str, _: dict = Depends(require_auth)) -> dict:
    result = await _queue_document_instance_reprocess(di_id)
    if result["status"] != "accepted":
        raise HTTPException(result["http_status"], result["detail"])
    trigger_results = await _trigger_reprocess_sources({result["source_id"]})
    trigger_reached = trigger_results.get(result["source_id"], False)
    return {
        "ok": True,
        "accepted": True,
        "mode": result["mode"],
        "status": "pending",
        "document_instance_id": di_id,
        "source_item_id": result["source_item_id"],
        "source_id": result["source_id"],
        "trigger_reached": trigger_reached,
    }


# ── Connector CRUD ────────────────────────────────────────────────────────────

class ConnectorCreate(BaseModel):
    folder_name: str
    type: str                    # rss | wechat
    config: dict[str, Any] = {}
    parent_folder_id: str | None = None


class ConnectorUpdate(BaseModel):
    config: dict[str, Any] | None = None
    status: str | None = None    # active | inactive


@connector_router.get("")
async def list_connectors(_: dict = Depends(require_auth)) -> list[dict]:
    rows = await database.database.fetch_all(
        """
        SELECT c.*, f.name as folder_name, f.status as folder_status
        FROM connectors c
        JOIN folders f ON f.id = c.folder_id
        WHERE c.user_id = :uid
        ORDER BY c.created_at DESC
        """,
        {"uid": USER_ID},
    )
    return [_serialize_timestamps(dict(r), "last_fetched_at", "created_at", "updated_at") for r in rows]


@connector_router.post("", status_code=201)
async def create_connector(body: ConnectorCreate, _: dict = Depends(require_auth)) -> dict:
    if body.type not in ("rss", "wechat"):
        raise HTTPException(400, "connector type 必须是 rss 或 wechat")

    # 创建 stream 资料夹（同时创建 legacy source）
    folder = await create_folder(
        FolderCreate(name=body.folder_name, parent_id=body.parent_folder_id, kind="stream"),
        _,
    )
    folder_id = folder["id"]

    con_id = _con_id(_source_id(folder_id))

    await database.database.execute(
        """
        INSERT INTO connectors (id, user_id, folder_id, type, config, status, created_at, updated_at)
        VALUES (:id, :uid, :fid, :type, :config, 'active', NOW(), NOW())
        """,
        {"id": con_id, "uid": USER_ID, "fid": folder_id,
         "type": body.type, "config": database.jsonb(body.config)},
    )

    # 更新 legacy source config（RSS URL 或 wechat feed_id）
    await database.database.execute(
        "UPDATE sources SET type = :type, config = :config, fetch_mode = 'subscription' WHERE id = :sid",
        {"type": body.type, "config": database.jsonb(body.config), "sid": _source_id(folder_id)},
    )

    row = await database.database.fetch_one(
        "SELECT c.*, f.name as folder_name FROM connectors c JOIN folders f ON f.id = c.folder_id WHERE c.id = :id",
        {"id": con_id},
    )
    assert row is not None
    return _serialize_timestamps(dict(row), "last_fetched_at", "created_at", "updated_at")


@connector_router.patch("/{connector_id}")
async def update_connector(
    connector_id: str,
    body: ConnectorUpdate,
    _: dict = Depends(require_auth),
) -> dict:
    row = await database.database.fetch_one(
        "SELECT * FROM connectors WHERE id = :id AND user_id = :uid",
        {"id": connector_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "connector 不存在")

    updates = ["updated_at = NOW()"]
    params: dict[str, Any] = {"id": connector_id}

    if body.config is not None:
        updates.append("config = :config")
        params["config"] = database.jsonb(body.config)
        await database.database.execute(
            "UPDATE sources SET config = :config WHERE id = :sid",
            {"config": database.jsonb(body.config), "sid": _source_id(row["folder_id"])},
        )
    if body.status in ("active", "inactive"):
        updates.append("status = :status")
        params["status"] = body.status

    await database.database.execute(
        f"UPDATE connectors SET {', '.join(updates)} WHERE id = :id", params
    )
    updated = await database.database.fetch_one(
        "SELECT c.*, f.name as folder_name FROM connectors c JOIN folders f ON f.id = c.folder_id WHERE c.id = :id",
        {"id": connector_id},
    )
    assert updated is not None
    return _serialize_timestamps(dict(updated), "last_fetched_at", "created_at", "updated_at")


@connector_router.delete("/{connector_id}", status_code=204)
async def delete_connector(connector_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT * FROM connectors WHERE id = :id AND user_id = :uid",
        {"id": connector_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "connector 不存在")
    await database.database.execute(
        "UPDATE connectors SET status = 'inactive', updated_at = NOW() WHERE id = :id",
        {"id": connector_id},
    )
    await database.database.execute(
        "UPDATE folders SET status = 'archived', updated_at = NOW() WHERE id = :fid",
        {"fid": row["folder_id"]},
    )


@connector_router.post("/{connector_id}/sync")
async def sync_connector(connector_id: str, _: dict = Depends(require_auth)) -> dict:
    row = await database.database.fetch_one(
        "SELECT * FROM connectors WHERE id = :id AND user_id = :uid",
        {"id": connector_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "connector 不存在")
    source_id = _source_id(row["folder_id"])
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5)
    except httpx.RequestError as e:
        raise HTTPException(502, f"无法连接 ingestion-worker: {e}")
    return {"ok": True}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None

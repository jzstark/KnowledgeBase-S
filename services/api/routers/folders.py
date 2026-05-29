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

import hashlib
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
from settings import settings

router = APIRouter(prefix="/api/folders", tags=["folders"])
di_router = APIRouter(prefix="/api/document-instances", tags=["document-instances"])
connector_router = APIRouter(prefix="/api/connectors", tags=["connectors"])

INGESTION_WORKER_URL = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")
WECHAT2RSS_BASE_URL = os.environ.get("WECHAT2RSS_BASE_URL", "https://rss.laughtale.co.uk/wechat-api")
WECHAT2RSS_FEED_BASE_URL = os.environ.get("WECHAT2RSS_FEED_BASE_URL", "https://rss.laughtale.co.uk/wechat")
WECHAT2RSS_TOKEN = os.environ.get("WECHAT2RSS_TOKEN", "")
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

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
            "SELECT COUNT(*) FROM document_instances WHERE folder_id = :fid",
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
    q = "SELECT di.*, an.node_id as article_id FROM document_instances di LEFT JOIN article_nodes an ON an.document_instance_id = di.id WHERE di.folder_id = :fid"
    params: dict[str, Any] = {"fid": folder_id}
    if status:
        q += " AND di.status = :status"
        params["status"] = status
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


@di_router.get("/{di_id}")
async def get_document_instance(di_id: str, _: dict = Depends(require_auth)) -> dict:
    row = await database.database.fetch_one(
        """
        SELECT di.*,
               ra.storage_key, ra.original_filename, ra.mime_type, ra.size, ra.sha256,
               an.node_id as article_id,
               kn.title as article_title
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

    updates = ["updated_at = NOW()"]
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
    if body.doc_kind is not None:
        updates.append("doc_kind = :doc_kind")
        params["doc_kind"] = _validate_doc_kind(body.doc_kind)

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


@di_router.delete("/{di_id}", status_code=204)
async def delete_document_instance(di_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT id FROM document_instances WHERE id = :id AND user_id = :uid",
        {"id": di_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "文档实例不存在")
    await database.database.execute(
        "UPDATE document_instances SET status = 'ignored', updated_at = NOW() WHERE id = :id",
        {"id": di_id},
    )


@di_router.post("/{di_id}/reprocess")
async def reprocess_document_instance(di_id: str, _: dict = Depends(require_auth)) -> dict:
    di = await database.database.fetch_one(
        "SELECT di.*, ra.storage_key FROM document_instances di LEFT JOIN raw_assets ra ON ra.id = di.raw_asset_id WHERE di.id = :id AND di.user_id = :uid",
        {"id": di_id, "uid": USER_ID},
    )
    if not di:
        raise HTTPException(404, "文档实例不存在")

    # 重置 document_instance 状态
    await database.database.execute(
        "UPDATE document_instances SET status = 'pending', updated_at = NOW() WHERE id = :id",
        {"id": di_id},
    )
    # 重置对应 source_item
    await database.database.execute(
        "UPDATE source_items SET status = 'pending', error = NULL, attempts = 0, updated_at = NOW() WHERE document_instance_id = :di_id",
        {"di_id": di_id},
    )

    # 找到对应 folder → source，触发 ingestion-worker
    if di["folder_id"]:
        source_id = _source_id(di["folder_id"])
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5)
        except Exception:
            pass

    return {"ok": True, "document_instance_id": di_id}


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

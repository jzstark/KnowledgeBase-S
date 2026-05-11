import hashlib
import os
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/sources", tags=["sources"])

INGESTION_WORKER_URL = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"


class SourceCreate(BaseModel):
    name: str
    type: str        # 'wechat'|'rss'|'url'|'pdf'|'image'|'plaintext'|'word'|'epub'
    config: dict[str, Any] = {}
    is_primary: bool = True


class SourceUpdate(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    is_primary: bool | None = None
    last_fetched_at: str | None = None   # ISO8601，worker 回写用


FETCH_MODES = {
    "wechat": "push",
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


class WechatIngestBody(BaseModel):
    source_id: str
    title: str
    content: str
    url: str = ""


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
           raw_retention_policy, status)
        VALUES
          (:id, :user_id, :source_id, :source_type, :origin_ref, :origin_ref_type,
           :raw_snapshot_ref, :extracted_text_ref, :content_hash, :title,
           :source_published_at, :source_updated_at, :captured_at, :effective_at,
           :raw_retention_policy, :status)
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
            "raw_retention_policy": item.raw_retention_policy,
            "status": item.status,
        },
    )
    return _serialize_source_item(row)


# ── 微信 push 端点（无需登录 cookie，靠 X-API-Token 鉴权）────────────────────

@router.post("/wechat/ingest")
async def wechat_ingest(request: Request, body: WechatIngestBody):
    """接收 iPhone 快捷指令推送的微信公众号文章，入库并触发 ingestion-worker。"""
    token = request.headers.get("X-API-Token", "")

    row = await database.database.fetch_one(
        "SELECT id, user_id, type, api_token, config, is_primary FROM sources WHERE id = :id",
        {"id": body.source_id},
    )
    if not row or row["type"] != "wechat":
        raise HTTPException(404, "source 不存在")
    if not secrets.compare_digest(token, row["api_token"] or ""):
        raise HTTPException(401, "token 无效")

    # 保存原始正文到 raw/wechat/
    raw_dir = USER_DATA_DIR / USER_ID / "raw" / "wechat"
    raw_dir.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.md5(body.content.encode()).hexdigest()[:8]
    file_name = f"{date.today()}-{content_hash}.txt"
    (raw_dir / file_name).write_text(body.content, encoding="utf-8")

    pushed_at = datetime.utcnow()
    await _create_source_item(
        row,
        SourceItemCreate(
            origin_ref=body.url or f"wechat://{file_name}",
            origin_ref_type="external",
            raw_snapshot_ref=str(raw_dir / file_name),
            content_hash=content_hash,
            title=body.title,
            captured_at=pushed_at,
            raw_retention_policy="keep_raw",
        ),
    )

    # 触发 ingestion-worker（fire-and-forget）
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{body.source_id}", timeout=5
            )
    except Exception:
        pass

    return {"ok": True}


@router.get("/{source_id}/source-items")
async def list_source_items(source_id: str, status: str | None = None, limit: int = 100):
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id", {"id": source_id}
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
async def create_source_items(source_id: str, body: SourceItemsCreate):
    row = await database.database.fetch_one(
        "SELECT id, user_id, type FROM sources WHERE id = :id", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    if not body.items:
        raise HTTPException(400, "items 不能为空")
    created = [await _create_source_item(row, item) for item in body.items]
    return {"ok": True, "items": created}


@router.post("/source-items/{item_id}/status")
async def update_source_item_status(item_id: str, body: SourceItemStatusUpdate):
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
    return _serialize_source_item(row)


@router.get("/{source_id}")
async def get_source(source_id: str):
    """获取单个 source 详情（含文章数）。"""
    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id", {"id": source_id}
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
    return d


@router.get("")
async def list_sources():
    """列出所有 sources（附带每个 source 的文章数）。"""
    rows = await database.database.fetch_all(
        "SELECT * FROM sources ORDER BY created_at DESC"
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
        result.append(d)
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, _: dict = Depends(require_auth)):
    if body.type not in FETCH_MODES:
        raise HTTPException(400, f"不支持的 source 类型: {body.type}")

    source_id = f"src_{secrets.token_hex(6)}"
    api_token = secrets.token_hex(16) if body.type == "wechat" else None

    await database.database.execute(
        """
        INSERT INTO sources (id, user_id, name, type, fetch_mode, is_primary, config, api_token)
        VALUES (:id, :user_id, :name, :type, :fetch_mode, :is_primary, :config, :api_token)
        """,
        {
            "id": source_id,
            "user_id": USER_ID,
            "name": body.name,
            "type": body.type,
            "fetch_mode": FETCH_MODES[body.type],
            "is_primary": body.is_primary,
            "config": database.jsonb(body.config),
            "api_token": api_token,
        },
    )
    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id", {"id": source_id}
    )
    d = dict(row)
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
    _: dict = Depends(require_auth),
):
    """向已有 source 上传一批文件（支持多文件），存储并触发 ingestion-worker 处理。
    Source 是持久渠道，可随时追加内容。"""
    row = await database.database.fetch_one(
        "SELECT id, user_id, type FROM sources WHERE id = :id", {"id": source_id}
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
                captured_at=datetime.fromisoformat(captured_at.replace("Z", "+00:00")) if captured_at else None,
                effective_at=datetime.fromisoformat(effective_at.replace("Z", "+00:00")) if effective_at else None,
                raw_retention_policy="keep_raw",
            ),
        )
        source_items.append(item)

    # 触发 ingestion-worker（fire-and-forget）
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5
            )
    except Exception:
        pass

    return {"ok": True, "files_saved": len(saved), "source_items": source_items}


@router.post("/{source_id}/add-url")
async def add_url_to_source(
    source_id: str,
    body: dict,
    _: dict = Depends(require_auth),
):
    """向已有 URL source 追加一条或多条 URL，触发 ingestion-worker 处理。"""
    row = await database.database.fetch_one(
        "SELECT id, user_id, type FROM sources WHERE id = :id", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    if row["type"] != "url":
        raise HTTPException(400, "仅 url 类型 source 支持此操作")

    urls: list[str] = body.get("urls", [])
    if not urls:
        raise HTTPException(400, "至少提供一个 URL")

    source_items = [
        await _create_source_item(
            row,
            SourceItemCreate(
                origin_ref=url,
                origin_ref_type="url",
                content_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                captured_at=datetime.utcnow(),
                raw_retention_policy="keep_extracted_only",
            ),
        )
        for url in urls
    ]

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5
            )
    except Exception:
        pass

    return {"ok": True, "urls_queued": len(urls), "source_items": source_items}


@router.post("/{source_id}/fetch")
async def trigger_fetch(source_id: str, _: dict = Depends(require_auth)):
    """触发 ingestion-worker 立即抓取指定 source。"""
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id", {"id": source_id}
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
async def update_source(source_id: str, body: SourceUpdate):
    row = await database.database.fetch_one(
        "SELECT id FROM sources WHERE id = :id", {"id": source_id}
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
    if body.last_fetched_at is not None:
        updates.append("last_fetched_at = :last_fetched_at")
        params["last_fetched_at"] = datetime.fromisoformat(body.last_fetched_at)

    if updates:
        await database.database.execute(
            f"UPDATE sources SET {', '.join(updates)} WHERE id = :id", params
        )

    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id", {"id": source_id}
    )
    d = dict(row)
    if d.get("last_fetched_at"):
        d["last_fetched_at"] = d["last_fetched_at"].isoformat()
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: str, _: dict = Depends(require_auth)):
    result = await database.database.execute(
        "DELETE FROM sources WHERE id = :id", {"id": source_id}
    )
    if result == 0:
        raise HTTPException(404, "source 不存在")

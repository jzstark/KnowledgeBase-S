import hashlib
import json
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


# ── 微信 push 端点（无需登录 cookie，靠 X-API-Token 鉴权）────────────────────

@router.post("/wechat/ingest")
async def wechat_ingest(request: Request, body: WechatIngestBody):
    """接收 iPhone 快捷指令推送的微信公众号文章，入库并触发 ingestion-worker。"""
    token = request.headers.get("X-API-Token", "")

    row = await database.database.fetch_one(
        "SELECT id, type, api_token, config, is_primary FROM sources WHERE id = :id",
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

    # 追加到 source.config.pending_items
    cfg = row["config"] or {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    cfg.setdefault("pending_items", []).append({
        "title": body.title,
        "url": body.url,
        "file_path": str(raw_dir / file_name),
        "pushed_at": datetime.utcnow().isoformat() + "Z",
    })
    await database.database.execute(
        "UPDATE sources SET config = :config WHERE id = :id",
        {"id": body.source_id, "config": database.jsonb(cfg)},
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
    _: dict = Depends(require_auth),
):
    """向已有 source 上传一批文件（支持多文件），存储并触发 ingestion-worker 处理。
    Source 是持久渠道，可随时追加内容。"""
    row = await database.database.fetch_one(
        "SELECT id, type FROM sources WHERE id = :id", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    src_type = row["type"]
    if src_type not in FILE_ACCEPT:
        raise HTTPException(400, f"source 类型 {src_type} 不支持文件上传")

    raw_dir = USER_DATA_DIR / USER_ID / "raw" / src_type
    raw_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for file in files:
        safe_name = f"{date.today()}-{secrets.token_hex(4)}-{file.filename or 'upload'}"
        file_path = raw_dir / safe_name
        content = await file.read()
        file_path.write_bytes(content)
        saved.append(str(file_path))

    # 将文件路径列表写入 source config（追加方式，保留历史）
    existing = await database.database.fetch_one(
        "SELECT config FROM sources WHERE id = :id", {"id": source_id}
    )
    import json as _json
    cfg = existing["config"] or {}
    if isinstance(cfg, str):
        cfg = _json.loads(cfg)
    uploads: list[dict] = cfg.get("uploads", [])
    uploads.append({"date": str(date.today()), "files": saved})
    cfg["uploads"] = uploads
    await database.database.execute(
        "UPDATE sources SET config = :config WHERE id = :id",
        {"id": source_id, "config": database.jsonb(cfg)},
    )

    # 触发 ingestion-worker（fire-and-forget）
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5
            )
    except Exception:
        pass

    return {"ok": True, "files_saved": len(saved)}


@router.post("/{source_id}/add-url")
async def add_url_to_source(
    source_id: str,
    body: dict,
    _: dict = Depends(require_auth),
):
    """向已有 URL source 追加一条或多条 URL，触发 ingestion-worker 处理。"""
    row = await database.database.fetch_one(
        "SELECT id, type, config FROM sources WHERE id = :id", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source 不存在")
    if row["type"] != "url":
        raise HTTPException(400, "仅 url 类型 source 支持此操作")

    import json as _json
    cfg = row["config"] or {}
    if isinstance(cfg, str):
        cfg = _json.loads(cfg)

    urls: list[str] = body.get("urls", [])
    if not urls:
        raise HTTPException(400, "至少提供一个 URL")

    pending: list[str] = cfg.get("pending_urls", [])
    pending.extend(urls)
    cfg["pending_urls"] = pending
    await database.database.execute(
        "UPDATE sources SET config = :config WHERE id = :id",
        {"id": source_id, "config": database.jsonb(cfg)},
    )

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{INGESTION_WORKER_URL}/trigger/{source_id}", timeout=5
            )
    except Exception:
        pass

    return {"ok": True, "urls_queued": len(urls)}


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

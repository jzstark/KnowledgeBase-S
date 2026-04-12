import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceCreate(BaseModel):
    name: str
    type: str        # 'wechat'|'rss'|'url'|'pdf'|'image'|'plaintext'|'word'
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
    "url": "one_shot",
    "pdf": "one_shot",
    "image": "one_shot",
    "plaintext": "one_shot",
    "word": "one_shot",
}


@router.get("")
async def list_sources():
    """列出所有 sources（worker 内网调用，无需认证）。"""
    rows = await database.database.fetch_all(
        "SELECT * FROM sources ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


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
            "user_id": "default",
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
    return dict(row)


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
        params["last_fetched_at"] = body.last_fetched_at

    if updates:
        await database.database.execute(
            f"UPDATE sources SET {', '.join(updates)} WHERE id = :id", params
        )

    row = await database.database.fetch_one(
        "SELECT * FROM sources WHERE id = :id", {"id": source_id}
    )
    return dict(row)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: str, _: dict = Depends(require_auth)):
    result = await database.database.execute(
        "DELETE FROM sources WHERE id = :id", {"id": source_id}
    )
    if result == 0:
        raise HTTPException(404, "source 不存在")

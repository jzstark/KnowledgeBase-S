import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])

USER_ID = "default"
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))

DEFAULT_SETTINGS = {
    "topics": "科技行业动态、AI 前沿、产品设计",
    "briefing_hours_back": 24,
    "briefing_time": "08:00",
    "maintenance_frequency": "weekly",
}


async def get_settings_dict() -> dict:
    row = await database.database.fetch_one(
        "SELECT settings FROM user_settings WHERE user_id = :user_id",
        {"user_id": USER_ID},
    )
    if not row:
        return DEFAULT_SETTINGS.copy()
    raw = row["settings"]
    data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return {**DEFAULT_SETTINGS, **data}


class SettingsUpdate(BaseModel):
    topics: str | None = None
    briefing_hours_back: int | None = None
    briefing_time: str | None = None
    maintenance_frequency: str | None = None


@router.get("")
async def get_settings(_: dict = Depends(require_auth)):
    return await get_settings_dict()


@router.put("")
async def update_settings(body: SettingsUpdate, _: dict = Depends(require_auth)):
    current = await get_settings_dict()
    updates = body.model_dump(exclude_none=True)
    merged = {**current, **updates}

    await database.database.execute(
        """
        INSERT INTO user_settings (user_id, settings)
        VALUES (:user_id, :settings)
        ON CONFLICT (user_id) DO UPDATE SET settings = :settings
        """,
        {"user_id": USER_ID, "settings": database.jsonb(merged)},
    )
    return merged


# ── 模板 CRUD ──────────────────────────────────────────────────────────────────

def _template_dir() -> Path:
    return USER_DATA_DIR / USER_ID / "config" / "templates"


@router.get("/templates")
async def list_templates(_: dict = Depends(require_auth)):
    """列出所有模板名称。"""
    d = _template_dir()
    if not d.exists():
        return []
    names = [p.stem for p in d.glob("*.md")] + [p.stem for p in d.glob("*.txt")]
    return sorted(set(names))


@router.get("/templates/{name}")
async def get_template(name: str, _: dict = Depends(require_auth)):
    """读取单个模板内容。"""
    d = _template_dir()
    for ext in (".md", ".txt"):
        p = d / f"{name}{ext}"
        if p.exists():
            return {"name": name, "content": p.read_text(encoding="utf-8")}
    raise HTTPException(404, "模板不存在")


class TemplateSave(BaseModel):
    content: str


@router.put("/templates/{name}")
async def save_template(name: str, body: TemplateSave, _: dict = Depends(require_auth)):
    """保存（新建或更新）模板。名称只允许字母/数字/下划线/中文/连字符。"""
    if not re.match(r"^[\w\u4e00-\u9fff\-]+$", name):
        raise HTTPException(400, "模板名称不合法")
    d = _template_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body.content, encoding="utf-8")
    return {"ok": True}


@router.delete("/templates/{name}")
async def delete_template(name: str, _: dict = Depends(require_auth)):
    """删除模板文件。"""
    d = _template_dir()
    for ext in (".md", ".txt"):
        p = d / f"{name}{ext}"
        if p.exists():
            p.unlink()
            return {"ok": True}
    raise HTTPException(404, "模板不存在")

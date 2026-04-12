import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])

USER_ID = "default"

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

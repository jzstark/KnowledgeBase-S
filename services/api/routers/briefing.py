"""
今日简报路由。

GET   /api/briefing                  — 获取今日（或指定日期）选题列表
POST  /api/briefing/generate         — 立即生成选题
PATCH /api/briefing/topics/{id}      — 更新选题状态（selected / skipped / pending）
"""

import json
import os
import secrets
from datetime import date, datetime, timedelta

import anthropic
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import database
import prompt_loader
from auth import require_auth
from routers.settings import get_settings_dict

router = APIRouter(prefix="/api/briefing", tags=["briefing"])

USER_ID = "default"
claude = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))


# ── 获取今日选题 ─────────────────────────────────────────────────────────────

@router.get("")
async def get_briefing(target_date: str | None = Query(default=None)):
    """
    返回指定日期的选题列表（默认今日）。
    响应格式：{date, topics: [{id, title, description, source_count, status}], generated}
    """
    d = date.fromisoformat(target_date) if target_date else date.today()

    rows = await database.database.fetch_all(
        "SELECT * FROM topics WHERE user_id = :user_id AND date = :date ORDER BY created_at ASC",
        {"user_id": USER_ID, "date": d},
    )

    if not rows:
        return {"date": str(d), "topics": [], "generated": False}

    topics = [
        {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "source_count": len(r["source_node_ids"] or []),
            "source_node_ids": list(r["source_node_ids"] or []),
            "status": r["status"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]

    return {
        "date": str(d),
        "topics": topics,
        "generated": True,
        "created_at": rows[0]["created_at"].isoformat(),
    }


# ── 生成选题 ─────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_briefing(_: dict = Depends(require_auth)):
    """立即生成今日选题：取最近 N 小时入库的节点 → Claude 生成写作角度 → 存库。"""
    settings = await get_settings_dict()
    hours_back = int(settings.get("briefing_hours_back", 24))
    topics_setting = settings.get("topics", "")

    # 1. 取最近 N 小时的主要节点
    since = (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
    rows = await database.database.fetch_all(
        f"""
        SELECT id, title, summary, tags, created_at
        FROM knowledge_nodes
        WHERE user_id = :user_id
          AND is_primary = true
          AND created_at >= '{since}'::timestamptz
        ORDER BY created_at DESC
        """,
        {"user_id": USER_ID},
    )

    today = date.today()

    # 先清除今日已有选题
    await database.database.execute(
        "DELETE FROM topics WHERE user_id = :user_id AND date = :date",
        {"user_id": USER_ID, "date": today},
    )

    if not rows:
        return {"date": str(today), "topics": [], "generated": True}

    node_list = [
        {
            "id": r["id"],
            "title": r["title"] or "",
            "summary": r["summary"] or "",
            "tags": list(r["tags"] or []),
        }
        for r in rows
    ]

    # 2. Claude 生成选题
    generated_topics = await _generate_topics(node_list, topics_setting)

    # 3. 存库
    result = []
    for t in generated_topics:
        topic_id = f"topic_{secrets.token_hex(6)}"
        source_ids = [
            node_list[i - 1]["id"]
            for i in t.get("source_indices", [])
            if 1 <= i <= len(node_list)
        ]
        await database.database.execute(
            """
            INSERT INTO topics (id, user_id, date, title, description, source_node_ids)
            VALUES (:id, :user_id, :date, :title, :description, :source_node_ids)
            """,
            {
                "id": topic_id,
                "user_id": USER_ID,
                "date": today,
                "title": t["title"],
                "description": t.get("description", ""),
                "source_node_ids": source_ids,
            },
        )
        result.append({
            "id": topic_id,
            "title": t["title"],
            "description": t.get("description", ""),
            "source_count": len(source_ids),
            "source_node_ids": source_ids,
            "status": "pending",
        })

    return {"date": str(today), "topics": result, "generated": True}


async def _generate_topics(nodes: list[dict], topics_setting: str) -> list[dict]:
    """用 Claude 基于今日原文生成写作选题角度，返回 [{title, description, source_indices}]。"""
    if not nodes:
        return []

    summaries = "\n".join(
        f"[{i+1}] {n['title']}：{n['summary'][:150]}"
        for i, n in enumerate(nodes)
    )

    prompt = prompt_loader.fill(
        "briefing_topics",
        topics_setting=topics_setting,
        summaries=summaries,
    )

    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw)


# ── 更新选题状态 ──────────────────────────────────────────────────────────────

class TopicStatusUpdate(BaseModel):
    status: str  # "selected" | "skipped" | "pending"


@router.patch("/topics/{topic_id}")
async def update_topic_status(topic_id: str, body: TopicStatusUpdate):
    await database.database.execute(
        "UPDATE topics SET status = :status WHERE id = :id AND user_id = :user_id",
        {"status": body.status, "id": topic_id, "user_id": USER_ID},
    )
    return {"ok": True}

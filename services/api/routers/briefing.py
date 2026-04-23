"""
今日简报路由。

GET   /api/briefing                  — 获取今日（或指定日期）选题列表
POST  /api/briefing/generate         — 立即生成选题
PATCH /api/briefing/topics/{id}      — 更新选题状态（selected / skipped / pending）
"""

import json
import logging
import os
import re
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
BATCH_SIZE = 12  # 初始批次大小；命中 max_tokens 时会自动对半拆分递归重试


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
async def generate_briefing(
    force: bool = Query(default=False),
    _: dict = Depends(require_auth),
):
    """立即生成今日选题。

    force=false（默认，增量模式）：
      - 首次：取最近 N 小时的节点全量生成
      - 再次：只处理上次生成后新入库的节点，追加到已有选题
      - 无新节点：直接返回已有选题，不调用 Claude

    force=true（重新生成模式，用于写作方向修改后）：
      - 删除今日已有选题，从最近 N 小时节点重新生成
    """
    settings = await get_settings_dict()
    hours_back = int(settings.get("briefing_hours_back", 24))
    topics_setting = settings.get("topics", "")
    today = date.today()

    if force:
        # 清空今日选题，从完整时间窗口重新生成
        await database.database.execute(
            "DELETE FROM topics WHERE user_id = :user_id AND date = :date",
            {"user_id": USER_ID, "date": today},
        )
        since = (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
        node_cutoff = f"created_at >= '{since}'::timestamptz"
    else:
        # 查询今日是否已有选题，有则只处理更新的节点
        last_topic = await database.database.fetch_one(
            "SELECT created_at FROM topics WHERE user_id = :user_id AND date = :date"
            " ORDER BY created_at DESC LIMIT 1",
            {"user_id": USER_ID, "date": today},
        )

        if last_topic:
            # 增量：只取上次生成之后新入库的节点
            since_dt = last_topic["created_at"]
            node_cutoff = f"created_at > '{since_dt.strftime('%Y-%m-%d %H:%M:%S')}'::timestamptz"
        else:
            # 今日首次生成：取最近 N 小时
            since = (datetime.utcnow() - timedelta(hours=hours_back)).strftime("%Y-%m-%d %H:%M:%S")
            node_cutoff = f"created_at >= '{since}'::timestamptz"

    rows = await database.database.fetch_all(
        f"""
        SELECT id, title, abstract, tags, created_at
        FROM knowledge_nodes
        WHERE user_id = :user_id
          AND is_primary = true
          AND object_type = 'article'
          AND {node_cutoff}
        ORDER BY created_at DESC
        """,
        {"user_id": USER_ID},
    )

    if rows:
        node_list = [
            {
                "id": r["id"],
                "title": r["title"] or "",
                "summary": r["abstract"] or "",
                "tags": list(r["tags"] or []),
            }
            for r in rows
        ]
        generated = await _generate_topics(node_list, topics_setting)
        for t in generated:
            topic_id = f"topic_{secrets.token_hex(6)}"
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
                    "source_node_ids": t.get("resolved_node_ids", []),
                },
            )

    # 返回今日全部选题（已有 + 新增）
    return await _fetch_today(today)


async def _fetch_today(today: date) -> dict:
    """返回指定日期的全部选题（格式与 GET /api/briefing 一致）。"""
    rows = await database.database.fetch_all(
        "SELECT * FROM topics WHERE user_id = :user_id AND date = :date ORDER BY created_at ASC",
        {"user_id": USER_ID, "date": today},
    )
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
    return {"date": str(today), "topics": topics, "generated": True}


def _repair_json_strings(s: str) -> str:
    """Escape unescaped newlines/tabs inside JSON string values."""
    result: list[str] = []
    in_string = False
    skip_next = False
    for ch in s:
        if skip_next:
            result.append(ch)
            skip_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            skip_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == "\n":
            result.append("\\n")
        elif in_string and ch == "\r":
            result.append("\\r")
        elif in_string and ch == "\t":
            result.append("\\t")
        else:
            result.append(ch)
    return "".join(result)


async def _generate_topics_batch(batch: list[dict], topics_setting: str) -> list[dict]:
    """单批次 Claude 调用。命中 max_tokens 时自动对半拆分递归重试，直到单篇为止。
    返回 [{title, description, resolved_node_ids}]，source_indices 已转换为实际 node ID。
    """
    if not batch:
        return []

    summaries = "\n".join(
        f"[{i+1}] {n['title']}：{n.get('summary', '')[:150]}"
        for i, n in enumerate(batch)
    )
    prompt = prompt_loader.fill(
        "briefing_topics",
        topics_setting=topics_setting,
        summaries=summaries,
    )
    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    if message.stop_reason == "max_tokens":
        if len(batch) == 1:
            logging.getLogger(__name__).error(
                "[briefing] max_tokens hit on single article '%s', skipping",
                batch[0].get("title", "?"),
            )
            return []
        mid = len(batch) // 2
        logging.getLogger(__name__).warning(
            "[briefing] max_tokens hit (batch=%d), splitting into %d + %d and retrying",
            len(batch), mid, len(batch) - mid,
        )
        left = await _generate_topics_batch(batch[:mid], topics_setting)
        right = await _generate_topics_batch(batch[mid:], topics_setting)
        return left + right

    raw = message.content[0].text.strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raw = _repair_json_strings(raw)
        parsed = json.loads(raw)

    # 将批次内 1-based 索引转换为实际 node ID
    for t in parsed:
        t["resolved_node_ids"] = [
            batch[i - 1]["id"]
            for i in t.get("source_indices", [])
            if 1 <= i <= len(batch)
        ]
    return parsed


async def _generate_topics(nodes: list[dict], topics_setting: str) -> list[dict]:
    """将节点按 BATCH_SIZE 分批，逐批调用 Claude，合并所有选题。"""
    all_topics: list[dict] = []
    for start in range(0, len(nodes), BATCH_SIZE):
        batch = nodes[start : start + BATCH_SIZE]
        all_topics.extend(await _generate_topics_batch(batch, topics_setting))
    return all_topics


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

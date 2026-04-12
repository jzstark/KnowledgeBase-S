"""
今日简报路由。

GET  /api/briefing        — 获取今日（或指定日期）简报
POST /api/briefing/generate — 立即生成简报（调用 summarizer 逻辑）
"""

import json
import os
from datetime import date, datetime, timedelta

import anthropic
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

import database
from auth import require_auth
from routers.settings import get_settings_dict

router = APIRouter(prefix="/api/briefing", tags=["briefing"])

USER_ID = "default"
claude = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))


# ── 获取简报 ────────────────────────────────────────────────────────────────

@router.get("")
async def get_briefing(target_date: str | None = Query(default=None)):
    """
    返回指定日期的简报（默认今日）。
    响应格式：{date, groups: [{name, nodes: [{id, title, summary, tags, edge_count}]}]}
    """
    d = date.fromisoformat(target_date) if target_date else date.today()

    row = await database.database.fetch_one(
        f"SELECT * FROM briefings WHERE user_id = :user_id AND date = '{d}'::date",
        {"user_id": USER_ID},
    )
    if not row:
        return {"date": str(d), "groups": [], "generated": False}

    groups_raw = row["groups"]
    groups = json.loads(groups_raw) if isinstance(groups_raw, str) else list(groups_raw)
    return {
        "date": str(d),
        "groups": groups,
        "generated": True,
        "created_at": row["created_at"].isoformat(),
    }


# ── 生成简报 ────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_briefing(_: dict = Depends(require_auth)):
    """立即生成今日简报：取最近 N 小时入库的节点 → Claude 分类 → 存库。"""
    settings = await get_settings_dict()
    hours_back = int(settings.get("briefing_hours_back", 24))
    topics = settings.get("topics", "")

    # 1. 取最近 N 小时的节点
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

    if not rows:
        groups: list[dict] = []
    else:
        # 2. 查每个节点的关联边数量
        node_list = []
        for r in rows:
            edge_count = await database.database.fetch_val(
                """
                SELECT COUNT(*) FROM knowledge_edges
                WHERE from_node_id = :id OR to_node_id = :id
                """,
                {"id": r["id"]},
            )
            node_list.append({
                "id": r["id"],
                "title": r["title"] or "",
                "summary": r["summary"] or "",
                "tags": r["tags"] or [],
                "edge_count": int(edge_count or 0),
                "created_at": r["created_at"].isoformat(),
            })

        # 3. Claude 分类
        groups = await _classify_nodes(node_list, topics)

    # 4. 存库（upsert 当日简报）
    today = date.today()
    groups_json = database.jsonb(groups)
    await database.database.execute(
        f"""
        INSERT INTO briefings (user_id, date, groups)
        VALUES (:user_id, '{today}'::date, :groups)
        ON CONFLICT (user_id, date) DO UPDATE SET groups = :groups, created_at = NOW()
        """,
        {"user_id": USER_ID, "groups": groups_json},
    )

    return {"date": str(today), "groups": groups, "generated": True}


async def _classify_nodes(nodes: list[dict], topics: str) -> list[dict]:
    """用 Claude 把节点按主题分组，返回 [{name, nodes}]。"""
    if not nodes:
        return []

    summaries = "\n".join(
        f"[{i+1}] {n['title']} — {n['summary'][:120]}"
        for i, n in enumerate(nodes)
    )

    prompt = f"""你是一个内容分类助手。用户的关注方向是：{topics}

以下是今日新增的文章列表（序号对应）：
{summaries}

请将这些文章按主题分组，每组给一个简短的中文名称（4字以内）。
优先按用户关注方向分组，无关内容归入"其他"。

严格按以下 JSON 格式输出，不要有其他文字：
[
  {{"name": "主题名", "indices": [1, 3, 5]}},
  {{"name": "主题名", "indices": [2, 4]}}
]"""

    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    classification = json.loads(raw)

    groups = []
    used_indices: set[int] = set()
    for group in classification:
        group_nodes = []
        for idx in group.get("indices", []):
            i = idx - 1
            if 0 <= i < len(nodes) and i not in used_indices:
                used_indices.add(i)
                group_nodes.append(nodes[i])
        if group_nodes:
            groups.append({"name": group["name"], "nodes": group_nodes})

    # 未分类的节点归入"其他"
    others = [nodes[i] for i in range(len(nodes)) if i not in used_indices]
    if others:
        groups.append({"name": "其他", "nodes": others})

    return groups

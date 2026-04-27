"""
草稿生成路由。

POST /api/drafts/generate   — RAG 检索 + 模板 + 偏好规则 → Claude 生成草稿
GET  /api/drafts            — 历史草稿列表（需认证）
GET  /api/drafts/{id}       — 单篇草稿详情（需认证）
"""

import os
import secrets
from pathlib import Path

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import config_loader
import database
from auth import require_auth
from routers.kb import _embed_query

router = APIRouter(prefix="/api/drafts", tags=["drafts"])

USER_ID = "default"
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
FEEDBACK_WORKER_URL = os.environ.get("FEEDBACK_WORKER_URL", "http://feedback-worker:8002")

claude = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))

DEFAULT_TEMPLATE = """请写一篇适合微信公众号的文章。风格轻松有观点，适合碎片化阅读。
开头用一个有趣的现象或问题引入，中间分2-3个小节展开，每节有小标题，
结尾给读者一个值得思考的问题，不要号召性语言。长度1500字左右。"""

MAX_KNOWLEDGE_CHARS = config_loader.get("retrieval.draft_knowledge_chars", 6000)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    selected_topic_ids: list[str]
    template_name: str = "default"


class FeedbackRequest(BaseModel):
    final_content: str


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def read_template(template_name: str) -> str:
    """读取用户模板文件，不存在则返回默认模板。"""
    template_dir = USER_DATA_DIR / USER_ID / "config" / "templates"
    candidates = [
        template_dir / f"{template_name}.md",
        template_dir / f"{template_name}.txt",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return DEFAULT_TEMPLATE


async def fetch_node(node_id: str) -> dict | None:
    row = await database.database.fetch_one(
        "SELECT id, title, abstract AS summary, tags FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    return dict(row) if row else None


async def fetch_topic(topic_id: str) -> dict | None:
    row = await database.database.fetch_one(
        "SELECT id, title, description, source_node_ids FROM topics WHERE id = :id",
        {"id": topic_id},
    )
    return dict(row) if row else None


async def semantic_search_related(query: str, exclude_ids: list[str], limit: int = 8) -> list[dict]:
    """语义检索相关节点，排除已选节点。"""
    embedding = await _embed_query(query)
    embedding_literal = "[" + ",".join(repr(x) for x in embedding) + "]"

    exclude_clause = ""
    if exclude_ids:
        ids_str = ", ".join(f"'{i}'" for i in exclude_ids)
        exclude_clause = f"AND id NOT IN ({ids_str})"

    async with database.database.connection() as conn:
        rows = await conn.raw_connection.fetch(
            f"""
            SELECT id, title, abstract AS summary, tags,
                   1 - (embedding <=> '{embedding_literal}'::vector) AS score
            FROM knowledge_nodes
            WHERE embedding IS NOT NULL
              AND user_id = '{USER_ID}'
              {exclude_clause}
            ORDER BY embedding <=> '{embedding_literal}'::vector
            LIMIT {limit}
            """
        )
    return [dict(r) for r in rows]


async def expand_one_hop(node_ids: list[str], relation_types: list[str]) -> list[dict]:
    """沿指定关系类型扩展一跳，返回邻居节点。"""
    if not node_ids:
        return []
    ids_str = ", ".join(f"'{i}'" for i in node_ids)
    edges = await database.database.fetch_all(
        f"""
        SELECT to_node_id, relation_type FROM knowledge_edges
        WHERE from_node_id IN ({ids_str})
          AND relation_type = ANY(:types)
        """,
        {"types": relation_types},
    )
    neighbor_ids = list({r["to_node_id"] for r in edges} - set(node_ids))
    if not neighbor_ids:
        return []

    ids_str2 = ", ".join(f"'{i}'" for i in neighbor_ids)
    rows = await database.database.fetch_all(
        f"SELECT id, title, abstract AS summary, tags FROM knowledge_nodes WHERE id IN ({ids_str2})"
    )
    return [dict(r) for r in rows]


def format_nodes(nodes: list[dict], label: str = "") -> str:
    lines = []
    if label:
        lines.append(f"【{label}】")
    for n in nodes:
        tags = "、".join((n.get("tags") or [])[:3])
        lines.append(f"- {n['title'] or '（无标题）'}（{tags}）\n  {(n.get('summary') or '')[:200]}")
    return "\n".join(lines)


def truncate_to_chars(text: str, max_chars: int) -> str:
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


# ── 端点 ──────────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_draft(body: GenerateRequest):
    """RAG 检索 + 模板 + 偏好规则 → Claude 生成草稿。"""
    if not body.selected_topic_ids:
        raise HTTPException(400, "至少选择一个选题")

    # 1. 获取已选选题
    topics = []
    for tid in body.selected_topic_ids:
        topic = await fetch_topic(tid)
        if topic:
            topics.append(topic)

    if not topics:
        raise HTTPException(404, "所选选题不存在")

    # 2. 通过选题的 source_node_ids 获取来源原文节点
    all_source_ids: list[str] = []
    for t in topics:
        all_source_ids.extend(t.get("source_node_ids") or [])
    all_source_ids = list(dict.fromkeys(all_source_ids))  # 去重保序

    source_nodes = []
    for nid in all_source_ids:
        node = await fetch_node(nid)
        if node:
            source_nodes.append(node)

    # 3. 语义检索更多相关知识（以选题标题+说明为查询）
    query = " ".join(
        f"{t['title']} {t.get('description', '')}" for t in topics
    )
    related = await semantic_search_related(query, all_source_ids, limit=8)

    # 4. 沿边扩展一跳（background_of / extends）
    related_ids = [n["id"] for n in related]
    extended = await expand_one_hop(related_ids, ["background_of", "extends"])

    # 5. 组合知识上下文，截断
    knowledge_text = format_nodes(related, "相关知识")
    if extended:
        knowledge_text += "\n\n" + format_nodes(extended, "背景知识")
    knowledge_text = truncate_to_chars(knowledge_text, MAX_KNOWLEDGE_CHARS)

    # 6. 读取偏好规则（confidence >= 0.8）
    pref_rows = await database.database.fetch_all(
        """
        SELECT rule FROM writing_memory
        WHERE user_id = :user_id
          AND (template_name = :tpl OR template_name IS NULL)
          AND confidence >= 0.8
        ORDER BY confidence DESC
        LIMIT 10
        """,
        {"user_id": USER_ID, "tpl": body.template_name},
    )
    preferences = "\n".join(f"- {r['rule']}" for r in pref_rows)

    # 7. 读取模板
    template = read_template(body.template_name)

    # 8. 组合 Prompt：选题角度在前，来源原文和背景知识在后
    topic_lines = "\n".join(
        f"- 【{t['title']}】{t.get('description', '')}" for t in topics
    )
    prompt_parts = [
        template,
        "",
        "本次写作的选题角度：",
        topic_lines,
    ]
    if source_nodes:
        prompt_parts += ["", "相关来源原文摘要：", format_nodes(source_nodes, "来源原文")]
    if knowledge_text:
        prompt_parts += ["", "知识库背景知识：", knowledge_text]
    if preferences:
        prompt_parts += ["", "根据用户历史反馈，额外注意：", preferences]
    prompt = "\n".join(prompt_parts)

    # 9. 调用 Claude
    message = claude.messages.create(
        model=config_loader.get("models.draft_generation", "claude-sonnet-4-6"),
        max_tokens=config_loader.get("llm_output_tokens.draft_generation", 4096),
        messages=[{"role": "user", "content": prompt}],
    )
    draft_content = message.content[0].text.strip()

    # 10. 写入 drafts 表
    draft_id = f"draft_{secrets.token_hex(6)}"
    await database.database.execute(
        """
        INSERT INTO drafts (id, user_id, template_name, selected_node_ids, selected_topic_ids, draft_content)
        VALUES (:id, :user_id, :template_name, :selected_node_ids, :selected_topic_ids, :draft_content)
        """,
        {
            "id": draft_id,
            "user_id": USER_ID,
            "template_name": body.template_name,
            "selected_node_ids": all_source_ids,
            "selected_topic_ids": body.selected_topic_ids,
            "draft_content": draft_content,
        },
    )

    return {
        "id": draft_id,
        "draft_content": draft_content,
        "template_name": body.template_name,
        "selected_count": len(topics),
        "knowledge_count": len(related) + len(extended),
    }


@router.get("")
async def list_drafts(_: dict = Depends(require_auth)):
    """历史草稿列表，不含正文。"""
    rows = await database.database.fetch_all(
        """
        SELECT id, template_name, selected_node_ids,
               LEFT(draft_content, 100) AS preview,
               created_at
        FROM drafts
        WHERE user_id = :user_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"user_id": USER_ID},
    )
    result = []
    for r in rows:
        d = dict(r)
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        result.append(d)
    return result


@router.post("/{draft_id}/feedback")
async def submit_feedback(draft_id: str, body: FeedbackRequest):
    """用户提交定稿，调用 feedback-worker 分析并学习偏好规则。"""
    row = await database.database.fetch_one(
        "SELECT id, template_name, draft_content FROM drafts WHERE id = :id AND user_id = :user_id",
        {"id": draft_id, "user_id": USER_ID},
    )
    if not row:
        raise HTTPException(404, "草稿不存在")

    # 保存定稿
    await database.database.execute(
        "UPDATE drafts SET final_content = :fc WHERE id = :id",
        {"fc": body.final_content, "id": draft_id},
    )

    # 同步调用 feedback-worker，取得学习到的规则数
    rules_extracted = 0
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{FEEDBACK_WORKER_URL}/analyze",
                json={
                    "draft_id": draft_id,
                    "draft_content": row["draft_content"] or "",
                    "final_content": body.final_content,
                    "template_name": row["template_name"] or "default",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                rules_extracted = resp.json().get("rules_extracted", 0)
    except Exception:
        pass  # feedback-worker 不可用时静默失败

    return {"ok": True, "rules_extracted": rules_extracted}


@router.get("/{draft_id}")
async def get_draft(draft_id: str, _: dict = Depends(require_auth)):
    """单篇草稿详情。"""
    row = await database.database.fetch_one(
        "SELECT * FROM drafts WHERE id = :id AND user_id = :user_id",
        {"id": draft_id, "user_id": USER_ID},
    )
    if not row:
        raise HTTPException(404, "草稿不存在")
    d = dict(row)
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d

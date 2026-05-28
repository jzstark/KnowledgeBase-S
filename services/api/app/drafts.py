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
from kb.public_service import fetch_node_light, fetch_reference_sources, layered_retrieval
from kb.wiki import read_wiki_body

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


async def fetch_topic(topic_id: str) -> dict | None:
    row = await database.database.fetch_one(
        "SELECT id, title, description, source_node_ids FROM topics WHERE id = :id",
        {"id": topic_id},
    )
    return dict(row) if row else None


def format_nodes(nodes: list[dict], label: str = "") -> str:
    lines = []
    if label:
        lines.append(f"【{label}】")
    for n in nodes:
        tags = "、".join((n.get("tags") or [])[:3])
        lines.append(f"- {n.get('title') or '（无标题）'}（{tags}）\n  {(n.get('summary') or '')[:200]}")
    return "\n".join(lines)


def truncate_to_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


def format_reference_section(references: list[dict]) -> str:
    if not references:
        return ""

    lines = ["", "---", "", "## 参考来源"]
    for index, ref in enumerate(references, start=1):
        title = ref.get("title") or ref.get("id") or "未命名来源"
        url = ref.get("url") or ""
        line = f"{index}. [{title}]({url})" if url else f"{index}. {title}"

        details = []
        if ref.get("source_name"):
            details.append(ref["source_name"])
        elif ref.get("source_type"):
            details.append(ref["source_type"])
        if ref.get("published_at"):
            details.append(ref["published_at"][:10])
        if details:
            line += f"（{'，'.join(details)}）"
        lines.append(line)
    return "\n".join(lines)



# ── 端点 ──────────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_draft(body: GenerateRequest):
    """RAG 检索（分层）+ 模板 + 偏好规则 → Claude 生成草稿。"""
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

    # 2. 来源原文节点（选题直接绑定的文章）
    all_source_ids: list[str] = list(dict.fromkeys(
        nid for t in topics for nid in (t.get("source_node_ids") or [])
    ))
    source_nodes = [n for nid in all_source_ids if (n := await fetch_node_light(nid))]

    # 3. 分层检索（排除已有来源节点）
    query = " ".join(f"{t['title']} {t.get('description', '')}" for t in topics)
    retrieval = await layered_retrieval(query, all_source_ids)

    # 4. 组装知识上下文（先文章后实体，token 预算递减）
    remaining = MAX_KNOWLEDGE_CHARS
    article_parts: list[str] = []
    for node in retrieval["articles"]:
        if remaining <= config_loader.get("drafts.min_remaining_chars", 100):
            break
        title   = node.get("title") or "（无标题）"
        tags    = "、".join((node.get("tags") or [])[:3])
        header  = f"**{title}**" + (f"（{tags}）" if tags else "")
        content = read_wiki_body(USER_ID, node["id"], node.get("object_type", "article"))
        if not content:
            content = node.get("summary") or ""
        content = truncate_to_chars(content, remaining - len(header) - 2)
        part    = f"{header}\n{content}" if content else header
        article_parts.append(part)
        remaining -= len(part) + 2  # +2 for separator

    entity_parts: list[str] = []
    for node in retrieval["entities"]:
        if remaining <= config_loader.get("drafts.min_remaining_chars", 100):
            break
        title   = node.get("title") or "（无名实体）"
        content = read_wiki_body(USER_ID, node["id"], "entity")
        if not content:
            content = node.get("summary") or ""
        content = truncate_to_chars(content, remaining - len(title) - 4)
        if content:
            entity_parts.append(f"**{title}**：{content}")
            remaining -= len(title) + len(content) + 4

    # 5. 读取偏好规则（confidence >= 0.8）
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

    # 6. 读取模板
    template = read_template(body.template_name)

    # 7. 组合 Prompt
    topic_lines = "\n".join(
        f"- 【{t['title']}】{t.get('description', '')}" for t in topics
    )
    prompt_parts = [template, "", "本次写作的选题角度：", topic_lines]
    if source_nodes:
        prompt_parts += ["", "相关来源原文摘要：", format_nodes(source_nodes, "来源原文")]
    if article_parts:
        prompt_parts += ["", "知识库相关文章：", "\n\n".join(article_parts)]
    if entity_parts:
        prompt_parts += ["", "相关实体：", "\n\n".join(entity_parts)]
    if preferences:
        prompt_parts += ["", "根据用户历史反馈，额外注意：", preferences]
    prompt = "\n".join(prompt_parts)

    # 8. 调用 Claude
    message = claude.messages.create(
        model=config_loader.get("models.draft_generation", "claude-sonnet-4-6"),
        max_tokens=config_loader.get("llm_output_tokens.draft_generation", 4096),
        messages=[{"role": "user", "content": prompt}],
    )
    draft_content = getattr(message.content[0], "text", "").strip()
    reference_node_ids = (
        all_source_ids
        + [node["id"] for node in retrieval["articles"]]
        + [node["id"] for node in retrieval["entities"]]
    )
    references = await fetch_reference_sources(reference_node_ids)
    draft_content += format_reference_section(references)

    # 9. 写入 drafts 表
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
        "knowledge_count": len(retrieval["articles"]) + len(retrieval["entities"]),
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

    await database.database.execute(
        "UPDATE drafts SET final_content = :fc WHERE id = :id",
        {"fc": body.final_content, "id": draft_id},
    )

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
        pass

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

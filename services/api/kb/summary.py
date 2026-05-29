"""
Summary management — generation, revision, and deletion of summary nodes.

Routes:
  POST /api/kb/nodes/{node_id}/create_summary   — enqueue summary generation job
  POST /api/kb/nodes/{node_id}/revise_summary   — enqueue revision job
  DELETE /api/kb/summaries/{summary_id}         — delete a summary node

Domain functions called by the job runner:
  generate_summary_job(node_id, perspective_label, perspective_instruction, user_id)
  revise_summary_job(node_id, instruction, perspective_label, perspective_instruction, user_id)
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
import jobs
import object_nodes
from settings import settings
from prompts import prompts
from auth import require_auth
from kb.common import USER_ID, _vector_literal
from kb.retrieval import _embed_text, claude_client
from kb.wiki import _wiki_file_path, write_wiki_node
from kb.ingest import _summary_perspective, build_similar_edges

router = APIRouter(prefix="/api/kb", tags=["KB Internal"])


# ── Models ────────────────────────────────────────────────────────────────────

class CreateSummaryRequest(BaseModel):
    perspective: str | None = None
    perspective_label: str | None = None
    perspective_instruction: str | None = None


class ReviseSummaryRequest(BaseModel):
    instruction: str
    perspective_label: str | None = None
    perspective_instruction: str | None = None


# ── Domain functions ──────────────────────────────────────────────────────────

async def generate_summary_job(
    node_id: str,
    perspective_label_input: str | None,
    perspective_instruction_input: str | None,
    user_id: str = USER_ID,
) -> dict[str, Any]:
    """Generate and persist a new summary node. Called by the job runner."""
    source = await database.database.fetch_one(
        """
        SELECT id, user_id, title, abstract, object_type, doc_kind
        FROM knowledge_nodes
        WHERE id = :id AND user_id = :user_id
        """,
        {"id": node_id, "user_id": user_id},
    )
    if not source:
        raise ValueError("节点不存在")
    if source["object_type"] not in ("article", "index"):
        raise ValueError("只能为 article 或 index 节点生成摘要")

    perspective_label, perspective_instruction, is_default = _summary_perspective(
        perspective_label_input, perspective_instruction_input, None,
    )
    prompt_perspective_instruction = (
        f"\n\n请从以下视角撰写摘要：{perspective_instruction}" if not is_default else ""
    )
    prompt = prompts.summary_gen(
        title=source["title"] or node_id,
        abstract=source["abstract"] or "",
        body=(source["abstract"] or "")[:3000],
        perspective_instruction=prompt_perspective_instruction,
    )
    message = await claude_client.messages.create(
        model=settings.models.summary_gen,
        max_tokens=settings.llm_output_tokens.summary_gen,
        messages=[{"role": "user", "content": prompt}],
    )
    summary_content = getattr(message.content[0], "text", "").strip()

    body_embedding = await _embed_text(summary_content)
    perspective_embedding = await _embed_text(f"{perspective_label}\n{perspective_instruction}")
    body_embedding_literal = _vector_literal(body_embedding)
    perspective_embedding_literal = _vector_literal(perspective_embedding)

    source_title = source["title"] or node_id
    summary_title = (
        f"{source_title} — {perspective_label}" if not is_default else f"{source_title} 摘要"
    )
    summary_id = f"sum_{secrets.token_hex(6)}"
    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, abstract, embedding, source_id,
           tags, object_type, doc_kind, embedding_model)
        VALUES
          (:id, :user_id, :title, :abstract, '{body_embedding_literal}'::vector,
           :source_id, :tags, :object_type, :doc_kind, :embedding_model)
        """,
        {
            "id": summary_id,
            "user_id": user_id,
            "title": summary_title,
            "abstract": summary_content,
            "source_id": node_id,
            "tags": [],
            "object_type": "summary",
            "doc_kind": source["doc_kind"] or settings.doc_kind.default,
            "embedding_model": settings.embedding.model,
        },
    )
    await object_nodes.upsert_object_node(
        summary_id,
        "summary",
        {
            "summary_of": node_id,
            "perspective_label": perspective_label,
            "perspective_instruction": perspective_instruction,
            "body": summary_content,
            "body_embedding_literal": body_embedding_literal,
            "perspective_embedding_literal": perspective_embedding_literal,
            "is_default": is_default,
            "source": {"source_node_ids": [node_id], "created_by": "job"},
        },
    )
    await write_wiki_node(summary_id, user_id)
    await build_similar_edges(summary_id, user_id)
    return {
        "id": summary_id,
        "title": summary_title,
        "content": summary_content,
        "perspective": None if is_default else perspective_label,
        "perspective_label": perspective_label,
        "perspective_instruction": perspective_instruction,
        "is_default": is_default,
        "source_id": node_id,
    }


async def revise_summary_job(
    node_id: str,
    instruction: str,
    perspective_label_input: str | None,
    perspective_instruction_input: str | None,
    user_id: str = USER_ID,
) -> dict[str, Any]:
    """Revise an existing summary node in place. Called by the job runner."""
    instruction = instruction.strip()
    if not instruction:
        raise ValueError("修改指令不能为空")

    summary = await database.database.fetch_one(
        """
        SELECT n.id, n.user_id, n.title, COALESCE(s.body, n.abstract) AS abstract,
               n.object_type, s.summary_of, s.perspective_label,
               s.perspective_instruction, s.is_default
        FROM knowledge_nodes n
        LEFT JOIN summary_nodes s ON s.node_id = n.id
        WHERE n.id = :id AND n.user_id = :user_id
        """,
        {"id": node_id, "user_id": user_id},
    )
    if not summary:
        raise ValueError("节点不存在")
    if summary["object_type"] != "summary":
        raise ValueError("只能 revise summary 节点")

    source_context = ""
    if summary["summary_of"]:
        source = await database.database.fetch_one(
            "SELECT id, title, abstract, object_type FROM knowledge_nodes WHERE id = :id",
            {"id": summary["summary_of"]},
        )
        if source:
            source_context = (
                f"对象类型：{source['object_type'] or ''}\n"
                f"标题：{source['title'] or source['id']}\n"
                f"系统摘要：{source['abstract'] or ''}"
            )

    prompt = (
        "你是一个知识库编辑助手。请根据用户指令修订已有 summary。\n\n"
        f"被观察对象：\n{source_context}\n\n"
        f"当前 summary：\n{summary['abstract'] or ''}\n\n"
        f"用户修改指令：\n{instruction}\n\n"
        "要求：\n"
        "- 只基于被观察对象和当前 summary 中已有事实修订，不要虚构新事实\n"
        "- 输出修订后的完整 summary 正文\n"
        "- 使用 3-6 句中文，纯文本输出，不含标题行或 Markdown 格式"
    )
    message = await claude_client.messages.create(
        model=settings.models.summary_gen,
        max_tokens=settings.llm_output_tokens.summary_gen,
        messages=[{"role": "user", "content": prompt}],
    )
    revised_content = getattr(message.content[0], "text", "").strip()

    body_embedding = await _embed_text(revised_content)
    body_embedding_literal = _vector_literal(body_embedding)

    perspective_changed = perspective_label_input is not None or perspective_instruction_input is not None
    perspective_label = summary["perspective_label"] or None
    perspective_instruction = summary["perspective_instruction"] or None
    is_default = bool(summary["is_default"])
    perspective_embedding_literal = None

    if perspective_changed:
        perspective_label, perspective_instruction, is_default = _summary_perspective(
            perspective_label_input, perspective_instruction_input, None,
        )
        perspective_embedding = await _embed_text(f"{perspective_label}\n{perspective_instruction}")
        perspective_embedding_literal = _vector_literal(perspective_embedding)

    await database.database.execute(
        f"""
        UPDATE knowledge_nodes
        SET abstract = :abstract,
            embedding = '{body_embedding_literal}'::vector,
            embedding_model = :embedding_model,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"id": node_id, "abstract": revised_content, "embedding_model": settings.embedding.model},
    )
    await object_nodes.upsert_object_node(
        node_id,
        "summary",
        {
            "summary_of": summary["summary_of"],
            "perspective_label": perspective_label,
            "perspective_instruction": perspective_instruction,
            "body": revised_content,
            "body_embedding_literal": body_embedding_literal,
            "perspective_embedding_literal": perspective_embedding_literal,
            "is_default": is_default,
            "source": {"source_node_ids": [summary["summary_of"]] if summary["summary_of"] else []},
        },
    )
    await database.database.execute(
        """
        DELETE FROM knowledge_edges
        WHERE relation_type = 'similar_to'
          AND created_by = 'auto_semantic'
          AND (from_node_id = :id OR to_node_id = :id)
        """,
        {"id": node_id},
    )
    await write_wiki_node(node_id, user_id)
    await build_similar_edges(node_id, user_id)
    return {
        "id": node_id,
        "title": summary["title"],
        "content": revised_content,
        "perspective": None if is_default else perspective_label,
        "perspective_label": perspective_label,
        "perspective_instruction": perspective_instruction,
        "is_default": is_default,
        "source_id": summary["summary_of"],
    }


async def do_delete_summary(summary_id: str) -> None:
    """Delete a summary node: wiki file, edges, and DB rows."""
    row = await database.database.fetch_one(
        "SELECT user_id, object_type FROM knowledge_nodes WHERE id = :id",
        {"id": summary_id},
    )
    if not row:
        raise ValueError("节点不存在")
    if row["object_type"] != "summary":
        raise ValueError("只能删除 summary 节点")

    uid = row["user_id"] or USER_ID
    wiki_file = _wiki_file_path(uid, summary_id, "summary")
    if wiki_file.exists():
        wiki_file.unlink()

    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": summary_id},
    )
    await database.database.execute(
        "DELETE FROM summary_nodes WHERE node_id = :id", {"id": summary_id},
    )
    await database.database.execute(
        "DELETE FROM knowledge_nodes WHERE id = :id", {"id": summary_id},
    )


# ── Route handlers ────────────────────────────────────────────────────────────

@router.post("/nodes/{node_id}/create_summary")
async def create_summary(node_id: str, body: CreateSummaryRequest):
    """Enqueue a summary generation job for an article or index node."""
    source = await database.database.fetch_one(
        "SELECT id, user_id, title, abstract, object_type FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    if not source:
        raise HTTPException(404, "节点不存在")
    if source["object_type"] not in ("article", "index"):
        raise HTTPException(400, "只能为 article 或 index 节点生成摘要")

    user_id = source["user_id"] or USER_ID
    perspective_label, perspective_instruction, _ = _summary_perspective(
        body.perspective_label, body.perspective_instruction, body.perspective,
    )
    key = f"generate_summary:{user_id}:{node_id}:{perspective_label}:{perspective_instruction}"
    job = await jobs.enqueue_job(
        "generate_summary",
        {"node_id": node_id, "perspective_label": perspective_label, "perspective_instruction": perspective_instruction},
        user_id=user_id,
        provider="anthropic",
        model=settings.models.summary_gen,
        priority=5,
        idempotency_key=key,
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.post("/nodes/{node_id}/revise_summary")
async def revise_summary(node_id: str, body: ReviseSummaryRequest):
    """Enqueue a summary revision job."""
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(400, "修改指令不能为空")

    summary = await database.database.fetch_one(
        "SELECT id, user_id, object_type FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    if not summary:
        raise HTTPException(404, "节点不存在")
    if summary["object_type"] != "summary":
        raise HTTPException(400, "只能 revise summary 节点")

    user_id = summary["user_id"] or USER_ID
    job = await jobs.enqueue_job(
        "revise_summary",
        {
            "node_id": node_id,
            "instruction": instruction,
            "perspective_label": body.perspective_label,
            "perspective_instruction": body.perspective_instruction,
        },
        user_id=user_id,
        provider="anthropic",
        model=settings.models.summary_gen,
        priority=5,
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.delete("/summaries/{summary_id}")
async def delete_summary(summary_id: str, _: dict = Depends(require_auth)):
    """Delete a summary node (wiki file + DB rows)."""
    try:
        await do_delete_summary(summary_id)
    except ValueError as e:
        msg = str(e)
        raise HTTPException(404 if "不存在" in msg else 400, msg)
    return {"ok": True}

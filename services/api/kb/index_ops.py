"""
Index management — create, update, and navigate hierarchical index nodes.

Routes:
  POST   /api/kb/indices
  GET    /api/kb/indices/{index_id}
  PATCH  /api/kb/indices/{index_id}
  GET    /api/kb/indices/{index_id}/children
  POST   /api/kb/indices/{index_id}/children
  DELETE /api/kb/indices/{index_id}/children/{child_id}
  PATCH  /api/kb/indices/{index_id}/children/order
  GET    /api/kb/objects/{object_id}/parents
  GET    /api/kb/objects/{object_id}/ancestors
  GET    /api/kb/indices/{index_id}/descendants
  POST   /api/kb/indices/{index_id}/rollup
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

import database
import jobs
from kb.graph import (
    add_child,
    fetch_node_with_object_fields,
    get_ancestors,
    get_children,
    get_descendants,
    get_parents,
    remove_child,
    reorder_children,
    upsert_object_node,
)
from settings import settings
from auth import require_auth
from kb.common import USER_ID
from kb.retrieval import _embed_text
from kb.wiki import write_wiki_index, write_wiki_node

router = APIRouter(prefix="/api/kb", tags=["KB Internal"])


# ── Models ────────────────────────────────────────────────────────────────────

class CreateIndexRequest(BaseModel):
    title: str
    description: str | None = None
    rollup_instruction: str | None = None
    tags: list[str] = []
    doc_kind: str | None = None


class UpdateIndexRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    rollup_instruction: str | None = None
    tags: list[str] | None = None


class AddIndexChildRequest(BaseModel):
    child_id: str
    position: int | None = None
    child_role: str = "member"


class ReorderIndexChildrenRequest(BaseModel):
    child_ids: list[str]


# ── Utilities ─────────────────────────────────────────────────────────────────

def _serialize_index_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        item = dict(row)
        for key in ("created_at", "updated_at"):
            if item.get(key):
                item[key] = item[key].isoformat()
        out.append(item)
    return out


async def _get_index_or_404(index_id: str) -> dict[str, Any]:
    node = await fetch_node_with_object_fields(index_id)
    if not node or node.get("object_type") != "index":
        raise HTTPException(404, "index 不存在")
    node.pop("embedding", None)
    node.pop("body_embedding", None)
    node.pop("perspective_embedding", None)
    for key in ("created_at", "updated_at", "ingested_at"):
        if node.get(key):
            node[key] = node[key].isoformat()
    return node


# ── Domain functions ──────────────────────────────────────────────────────────

async def do_create_index(body: CreateIndexRequest) -> str:
    """Create an index node; return its new ID."""
    title = body.title.strip()
    if not title:
        raise ValueError("title 不能为空")
    description = (body.description or "").strip()
    doc_kind = body.doc_kind or settings.doc_kind.default
    allowed = set(settings.doc_kind.values)
    if allowed and doc_kind not in allowed:
        raise ValueError(f"无效的 doc_kind；可选值：{', '.join(sorted(allowed))}")

    embedding = await _embed_text("\n".join([title, description]).strip() or title)
    from kb.common import _vector_literal
    embedding_literal = _vector_literal(embedding)
    index_id = f"idx_{secrets.token_hex(6)}"
    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, abstract, embedding, source_id,
           tags, object_type, doc_kind, embedding_model)
        VALUES
          (:id, :user_id, :title, :abstract, '{embedding_literal}'::vector,
           :source_id, :tags, 'index', :doc_kind, :embedding_model)
        """,
        {
            "id": index_id,
            "user_id": USER_ID,
            "title": title,
            "abstract": description,
            "source_id": index_id,
            "tags": body.tags,
            "doc_kind": doc_kind,
            "embedding_model": settings.embedding.model,
        },
    )
    await upsert_object_node(
        index_id,
        "index",
        {
            "description": description,
            "rollup_instruction": body.rollup_instruction,
            "abstract_stale": False,
        },
    )
    return index_id


async def do_update_index(index_id: str, body: UpdateIndexRequest) -> None:
    """Apply metadata updates to an index node."""
    current = await _get_index_or_404(index_id)
    title = body.title.strip() if body.title is not None else current.get("title")
    if not title:
        raise ValueError("title 不能为空")
    description = body.description if body.description is not None else current.get("description")
    rollup_instruction = (
        body.rollup_instruction
        if body.rollup_instruction is not None
        else current.get("rollup_instruction")
    )
    tags = body.tags if body.tags is not None else current.get("tags") or []
    abstract_stale = body.description is not None or body.rollup_instruction is not None

    await database.database.execute(
        """
        UPDATE knowledge_nodes
        SET title = :title, abstract = :description, tags = :tags, updated_at = NOW()
        WHERE id = :id AND user_id = :user_id AND object_type = 'index'
        """,
        {"id": index_id, "user_id": USER_ID, "title": title, "description": description or "", "tags": tags},
    )
    await upsert_object_node(
        index_id,
        "index",
        {
            "description": description,
            "rollup_instruction": rollup_instruction,
            "abstract_stale": abstract_stale or bool(current.get("abstract_stale")),
        },
    )


# ── Route handlers ────────────────────────────────────────────────────────────

@router.post("/indices")
async def create_index(body: CreateIndexRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        index_id = await do_create_index(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    background_tasks.add_task(write_wiki_index, USER_ID)
    return await _get_index_or_404(index_id)


@router.get("/indices/{index_id}")
async def get_index(index_id: str):
    return await _get_index_or_404(index_id)


@router.patch("/indices/{index_id}")
async def update_index(index_id: str, body: UpdateIndexRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        await do_update_index(index_id, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    background_tasks.add_task(write_wiki_index, USER_ID)
    return await _get_index_or_404(index_id)


@router.get("/indices/{index_id}/children")
async def get_index_children(index_id: str):
    await _get_index_or_404(index_id)
    return {"children": _serialize_index_rows(await get_children(index_id, USER_ID))}


@router.post("/indices/{index_id}/children")
async def add_index_child(index_id: str, body: AddIndexChildRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        child = await add_child(
            index_id, body.child_id, user_id=USER_ID, position=body.position, child_role=body.child_role,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    return child


@router.delete("/indices/{index_id}/children/{child_id}")
async def remove_index_child(index_id: str, child_id: str, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        deleted = await remove_child(index_id, child_id, USER_ID)
    except ValueError as e:
        raise HTTPException(404, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    return {"ok": True, "deleted": deleted}


@router.patch("/indices/{index_id}/children/order")
async def reorder_index_children(index_id: str, body: ReorderIndexChildrenRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        await reorder_children(index_id, body.child_ids, USER_ID)
    except ValueError as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    return {"ok": True, "children": _serialize_index_rows(await get_children(index_id, USER_ID))}


@router.get("/objects/{object_id}/parents")
async def get_object_parents(object_id: str):
    return {"parents": _serialize_index_rows(await get_parents(object_id, USER_ID))}


@router.get("/objects/{object_id}/ancestors")
async def get_object_ancestors(object_id: str):
    return {"ancestors": _serialize_index_rows(await get_ancestors(object_id, USER_ID))}


@router.get("/indices/{index_id}/descendants")
async def get_index_descendants(index_id: str):
    await _get_index_or_404(index_id)
    return {"descendants": _serialize_index_rows(await get_descendants(index_id, USER_ID))}


@router.post("/indices/{index_id}/rollup")
async def rollup_index(index_id: str, _: dict = Depends(require_auth)):
    await _get_index_or_404(index_id)
    job = await jobs.enqueue_job(
        "aggregate_index_abstract",
        {"index_id": index_id, "only_stale": False},
        user_id=USER_ID,
        provider="anthropic",
        model=settings.models.index_summary,
        priority=10,
        idempotency_key=f"aggregate_index_abstract:{USER_ID}:{index_id}",
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}

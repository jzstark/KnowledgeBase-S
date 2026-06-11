"""
Core node operations — search, node detail, node CRUD, graph views,
background job management, and maintenance triggers.

Routes:
  GET    /api/kb/search
  GET    /api/kb/node/{node_id}
  DELETE /api/kb/nodes/{node_id}
  POST   /api/kb/nodes/{node_id}/archive
  PATCH  /api/kb/nodes/{node_id}/metadata
  GET    /api/kb/graph
  GET    /api/kb/nodes
  GET    /api/kb/graph/all
  POST   /api/kb/wiki/rebuild
  GET    /api/kb/wiki/status
  GET    /api/kb/jobs
  GET    /api/kb/jobs/{job_id}
  POST   /api/kb/jobs/{job_id}/cancel
  POST   /api/kb/jobs/{job_id}/retry
  POST   /api/kb/maintenance/run
  POST   /api/kb/maintenance/rebuild_from_raw
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import database
import jobs
from kb.graph import fetch_node_with_object_fields
from settings import settings
from auth import require_auth, require_auth_or_service_token
from kb.common import USER_DATA_DIR, USER_ID, _is_visible_edge, _vector_literal, split_frontmatter
from kb.retrieval import embed_text
from kb.wiki import _wiki_file_path

router = APIRouter(prefix="/api/kb", tags=["KB Internal"])


# ── Models ────────────────────────────────────────────────────────────────────

class UpdateNodeMetadataRequest(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    published_at: datetime | None = None
    doc_kind: str | None = None


class RebuildFromRawRequest(BaseModel):
    source_id: str | None = None
    source_type: str | None = None
    status: str | None = None
    since: str | None = None
    until: str | None = None
    dry_run: bool = False
    resume: bool = False


# ── Domain functions ──────────────────────────────────────────────────────────

async def do_update_node_metadata(node_id: str, body: UpdateNodeMetadataRequest) -> None:
    """Apply title / tags / published_at / doc_kind updates to any node."""
    row = await database.database.fetch_one(
        "SELECT object_type FROM knowledge_nodes WHERE id = :id", {"id": node_id},
    )
    if not row:
        raise ValueError("节点不存在")

    if body.doc_kind is not None:
        allowed = settings.doc_kind.values
        if body.doc_kind not in allowed:
            raise ValueError(f"无效的 doc_kind；可选值：{', '.join(allowed)}")

    updates: list[str] = []
    params: dict[str, Any] = {"id": node_id}
    if body.title is not None:
        updates.append("title = :title")
        params["title"] = body.title.strip()
    if body.tags is not None:
        updates.append("tags = :tags")
        params["tags"] = body.tags
    if body.published_at is not None:
        updates.append("published_at = :published_at")
        params["published_at"] = body.published_at
    if body.doc_kind is not None:
        updates.append("doc_kind = :doc_kind")
        params["doc_kind"] = body.doc_kind

    if updates:
        await database.database.execute(
            f"UPDATE knowledge_nodes SET {', '.join(updates)}, updated_at = NOW() WHERE id = :id",
            params,
        )
    # Keep entity_nodes.canonical_name in sync when entity title changes
    if body.title is not None and row["object_type"] == "entity":
        await database.database.execute(
            "UPDATE entity_nodes SET canonical_name = :title WHERE node_id = :id",
            {"title": body.title.strip(), "id": node_id},
        )


# ── Route handlers ────────────────────────────────────────────────────────────

@router.get("/search")
async def search(
    q: str,
    limit: int = Query(10, ge=1, le=50),
    tags: str | None = None,
    type: str | None = None,
    _: dict = Depends(require_auth_or_service_token),
):
    """Semantic search (RAG core call). Uses raw embedding, no HyDE."""
    embedding = await embed_text(q)
    embedding_literal = _vector_literal(embedding)

    tag_list: list[str] = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    conditions = ["(n.embedding IS NOT NULL OR s.body_embedding IS NOT NULL)"]
    extra_params: list = []

    if tag_list:
        placeholders = ", ".join(f"${len(extra_params) + i + 1}" for i in range(len(tag_list)))
        conditions.append(f"n.tags && ARRAY[{placeholders}]::text[]")
        extra_params.extend(tag_list)

    if type:
        ti = len(extra_params) + 1
        conditions.append(f"n.object_type = ${ti}")
        extra_params.append(type)

    where = " AND ".join(conditions)
    limit_idx = len(extra_params) + 1

    async with database.database.connection() as conn:
        sql = f"""
            SELECT n.id, n.user_id, n.title, COALESCE(s.body, n.abstract) AS abstract,
                   COALESCE(an.source_type, n.object_type) AS source_type,
                   n.tags, n.object_type, n.created_at,
                   s.perspective_label, s.perspective_instruction, s.is_default,
                   CASE
                     WHEN n.object_type = 'summary' THEN
                       0.75 * (1 - (COALESCE(s.body_embedding, n.embedding) <=> '{embedding_literal}'::vector))
                       + 0.25 * (1 - (COALESCE(s.perspective_embedding, s.body_embedding, n.embedding) <=> '{embedding_literal}'::vector))
                     ELSE
                       1 - (n.embedding <=> '{embedding_literal}'::vector)
                   END AS score
            FROM knowledge_nodes n
            LEFT JOIN summary_nodes s ON s.node_id = n.id
            LEFT JOIN article_nodes an ON an.node_id = n.id
            WHERE {where}
            ORDER BY score DESC
            LIMIT ${limit_idx}
        """
        rows = await conn.raw_connection.fetch(sql, *extra_params, limit)

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "abstract": r["abstract"],
            "source_type": r["source_type"],
            "tags": r["tags"],
            "object_type": r["object_type"],
            "score": float(r["score"]),
            "perspective_label": r["perspective_label"],
            "perspective_instruction": r["perspective_instruction"],
            "is_default": bool(r["is_default"]) if r["is_default"] is not None else False,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.get("/node/{node_id}")
async def get_node(node_id: str, _: dict = Depends(require_auth_or_service_token)):
    """Fetch a single node with all its edges."""
    node = await fetch_node_with_object_fields(node_id)
    if not node:
        raise HTTPException(404, "节点不存在")

    edges = await database.database.fetch_all(
        "SELECT * FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": node_id},
    )
    structure_rows = await database.database.fetch_all(
        """
        SELECT index_id AS from_node_id, child_id AS to_node_id
        FROM index_children
        WHERE index_id = :id OR child_id = :id
        ORDER BY position ASC, created_at ASC
        """,
        {"id": node_id},
    )

    node.pop("embedding", None)
    node.pop("body_embedding", None)
    node.pop("perspective_embedding", None)
    if node.get("raw_ref") and isinstance(node["raw_ref"], str):
        node["raw_ref"] = json.loads(node["raw_ref"])
    for key in ("created_at", "updated_at", "ingested_at", "published_at",
                "source_published_at", "source_updated_at", "captured_at", "effective_at"):
        if node.get(key):
            node[key] = node[key].isoformat()

    wiki_body = ""
    object_type = node.get("object_type") or "article"
    wiki_file = _wiki_file_path(node.get("user_id") or USER_ID, node_id, object_type)
    if wiki_file.exists():
        raw_wiki = wiki_file.read_text(encoding="utf-8")
        body_section = split_frontmatter(raw_wiki)[1].strip()
        if body_section:
            for sentinel in ("\n## 関連節点\n", "\n## 关联节点\n"):
                if sentinel in body_section:
                    body_section = body_section[: body_section.index(sentinel)].strip()
            lines = body_section.split("\n", 2)
            wiki_body = lines[2].strip() if len(lines) >= 3 else body_section

    return {
        **node,
        "wiki_body": wiki_body,
        "edges": [dict(e) for e in edges if _is_visible_edge(e["relation_type"])]
        + [
            {
                "id": -(i + 1),
                "from_node_id": r["from_node_id"],
                "to_node_id": r["to_node_id"],
                "relation_type": "contains",
                "weight": 1.0,
                "created_by": "index_children",
            }
            for i, r in enumerate(structure_rows)
        ],
    }


async def do_delete_node(node_id: str) -> bool:
    """Delete a node (wiki file, edges, DB row). Returns False if it didn't exist.

    CASCADE on knowledge_nodes(id) cleans the object sub-tables (article_nodes,
    summary_nodes, entity_facts, index_children, …). NOTE: deleting an *article*
    only cascades the summary_nodes row via summary_of — the summary's own
    knowledge_node is a separate row and must be deleted explicitly (see the
    hard-delete path in routers/folders.py).
    """
    row = await database.database.fetch_one(
        "SELECT user_id, object_type FROM knowledge_nodes WHERE id = :id", {"id": node_id},
    )
    if not row:
        return False

    wiki_file = _wiki_file_path(row["user_id"] or USER_ID, node_id, row["object_type"] or "article")
    if wiki_file.exists():
        wiki_file.unlink()

    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id", {"id": node_id},
    )
    await database.database.execute(
        "DELETE FROM knowledge_nodes WHERE id = :id", {"id": node_id},
    )
    return True


@router.delete("/nodes/{node_id}")
async def delete_node(node_id: str, _: dict = Depends(require_auth)):
    """Delete a node: wiki file, edges, DB record."""
    if not await do_delete_node(node_id):
        raise HTTPException(404, "节点不存在")
    return {"ok": True}


@router.post("/nodes/{node_id}/archive")
async def archive_node(node_id: str, _: dict = Depends(require_auth)):
    """Soft-delete an article node (sets article_nodes.status = 'archived')."""
    row = await database.database.fetch_one(
        "SELECT object_type FROM knowledge_nodes WHERE id = :id", {"id": node_id},
    )
    if not row:
        raise HTTPException(404, "节点不存在")
    if row["object_type"] != "article":
        raise HTTPException(400, "archive 仅适用于 article 节点")
    await database.database.execute(
        "UPDATE article_nodes SET status = 'archived' WHERE node_id = :id", {"id": node_id},
    )
    return {"ok": True}


@router.patch("/nodes/{node_id}/metadata")
async def update_node_metadata(node_id: str, body: UpdateNodeMetadataRequest, _: dict = Depends(require_auth)):
    """Update title, tags, published_at, or doc_kind for any node."""
    try:
        await do_update_node_metadata(node_id, body)
    except ValueError as e:
        msg = str(e)
        raise HTTPException(404 if "不存在" in msg else 400, msg)
    return {"ok": True}


@router.get("/graph")
async def get_graph(root: str, depth: int = Query(2, ge=1, le=3)):
    """BFS from root node; return node set + edge set. No auth required."""
    visited_nodes: set[str] = set()
    visited_edges: set[int] = set()
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    nodes_out: list[dict] = []
    edges_out: list[dict] = []

    while queue:
        node_id, current_depth = queue.popleft()
        if node_id in visited_nodes:
            continue
        visited_nodes.add(node_id)

        row = await database.database.fetch_one(
            """
            SELECT n.id, n.title, n.abstract,
                   COALESCE(an.source_type, n.object_type) AS source_type,
                   n.tags, n.object_type, n.created_at
            FROM knowledge_nodes n
            LEFT JOIN article_nodes an ON an.node_id = n.id
            WHERE n.id = :id
            """,
            {"id": node_id},
        )
        if not row:
            continue
        n = dict(row)
        if n.get("created_at"):
            n["created_at"] = n["created_at"].isoformat()
        nodes_out.append(n)

        if current_depth >= depth:
            continue

        edge_rows = await database.database.fetch_all(
            "SELECT * FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
            {"id": node_id},
        )
        structure_rows = await database.database.fetch_all(
            """
            SELECT index_id AS from_node_id, child_id AS to_node_id
            FROM index_children
            WHERE index_id = :id OR child_id = :id
            ORDER BY position ASC, created_at ASC
            """,
            {"id": node_id},
        )
        for e in edge_rows:
            ed = dict(e)
            if not _is_visible_edge(ed["relation_type"]):
                continue
            if ed["id"] not in visited_edges:
                visited_edges.add(ed["id"])
                edges_out.append(ed)
            neighbor = ed["to_node_id"] if ed["from_node_id"] == node_id else ed["from_node_id"]
            if neighbor not in visited_nodes:
                queue.append((neighbor, current_depth + 1))
        for i, r in enumerate(structure_rows):
            ed = {
                "id": -((len(edges_out) + i + 1)),
                "from_node_id": r["from_node_id"],
                "to_node_id": r["to_node_id"],
                "relation_type": "contains",
                "weight": 1.0,
            }
            if ed["id"] not in visited_edges:
                visited_edges.add(ed["id"])
                edges_out.append(ed)
            neighbor = ed["to_node_id"] if ed["from_node_id"] == node_id else ed["from_node_id"]
            if neighbor not in visited_nodes:
                queue.append((neighbor, current_depth + 1))

    return {"nodes": nodes_out, "edges": edges_out}


@router.get("/nodes")
async def list_nodes(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tags: str | None = None,
    q: str | None = None,
    type: str | None = None,
    source_id: str | None = None,
):
    """Paginated node list with text search, tag, type, and source filters. No auth required."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    conditions = ["n.user_id = $1"]
    params: list = [USER_ID]

    if tag_list:
        placeholders = ", ".join(f"${i + 2}" for i in range(len(tag_list)))
        conditions.append(f"n.tags && ARRAY[{placeholders}]::text[]")
        params.extend(tag_list)
    if q and q.strip():
        qi = len(params) + 1
        conditions.append(f"(n.title ILIKE ${qi} OR n.abstract ILIKE ${qi})")
        params.append(f"%{q.strip()}%")
    if type:
        ti = len(params) + 1
        conditions.append(f"n.object_type = ${ti}")
        params.append(type)
    if source_id:
        si = len(params) + 1
        conditions.append(f"n.source_id = ${si}")
        params.append(source_id)

    where = " AND ".join(conditions)
    limit_idx = len(params) + 1
    offset_idx = len(params) + 2

    async with database.database.connection() as conn:
        total = await conn.raw_connection.fetchval(
            f"SELECT COUNT(*) FROM knowledge_nodes n WHERE {where}", *params
        )
        rows = await conn.raw_connection.fetch(
            f"""
            SELECT n.id, n.title, n.abstract,
                   COALESCE(an.source_type, n.object_type) AS source_type,
                   n.tags, n.object_type, n.created_at,
                   COALESCE(n.published_at, n.ingested_at, n.created_at) AS published_at,
                   n.source_id, n.doc_kind,
                   s.name AS source_name,
                   s.deleted_at AS source_deleted_at
            FROM knowledge_nodes n
            LEFT JOIN article_nodes an ON an.node_id = n.id
            LEFT JOIN sources s ON s.id = n.source_id
            WHERE {where}
            ORDER BY COALESCE(n.published_at, n.ingested_at, n.created_at) DESC NULLS LAST
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params, limit, offset,
        )

    return {
        "nodes": [
            {
                "id": r["id"],
                "title": r["title"],
                "abstract": r["abstract"],
                "source_type": r["source_type"],
                "tags": list(r["tags"]) if r["tags"] else [],
                "object_type": r["object_type"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                "source_id": r["source_id"],
                "source_name": r["source_name"],
                "source_deleted_at": r["source_deleted_at"].isoformat() if r["source_deleted_at"] else None,
                "doc_kind": r["doc_kind"],
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/graph/all")
async def get_full_graph(limit: int = Query(1000, ge=1, le=2000), type: str | None = None):
    """Full node + edge dump for D3 force-directed graph. No auth required."""
    type_filter = "AND n.object_type = $2" if type else ""
    params = [USER_ID, limit] if not type else [USER_ID, type, limit]
    limit_idx = len(params)

    async with database.database.connection() as conn:
        node_rows = await conn.raw_connection.fetch(
            f"""
            SELECT n.id, n.title, COALESCE(an.source_type, n.object_type) AS source_type,
                   n.tags, n.object_type,
                   COUNT(e.id) FILTER (
                     WHERE e.relation_type NOT IN ('extends', 'background_of', 'supports', 'contradicts', 'part_of')
                   )::int AS degree
            FROM knowledge_nodes n
            LEFT JOIN article_nodes an ON an.node_id = n.id
            LEFT JOIN knowledge_edges e ON e.from_node_id = n.id OR e.to_node_id = n.id
            WHERE n.user_id = $1 {type_filter}
            GROUP BY n.id, an.source_type
            ORDER BY n.created_at DESC
            LIMIT ${limit_idx}
            """,
            *params,
        )
        node_ids = [r["id"] for r in node_rows]
        edge_rows = []
        if node_ids:
            edge_rows = await conn.raw_connection.fetch(
                """
                SELECT id, from_node_id, to_node_id, relation_type, weight
                FROM knowledge_edges
                WHERE relation_type NOT IN ('extends', 'background_of', 'supports', 'contradicts', 'part_of')
                  AND from_node_id = ANY($1::varchar[])
                  AND to_node_id = ANY($1::varchar[])
                """,
                node_ids,
            )
        structure_rows = await conn.raw_connection.fetch(
            """
            SELECT ROW_NUMBER() OVER (ORDER BY index_id, position, child_id) AS rn,
                   index_id AS from_node_id, child_id AS to_node_id
            FROM index_children
            LIMIT 1000
            """
        )

    node_ids = {r["id"] for r in node_rows}
    return {
        "nodes": [
            {
                "id": r["id"],
                "title": r["title"],
                "source_type": r["source_type"],
                "tags": list(r["tags"]) if r["tags"] else [],
                "object_type": r["object_type"],
                "degree": r["degree"],
            }
            for r in node_rows
        ],
        "edges": [
            {
                "id": e["id"],
                "from_node_id": e["from_node_id"],
                "to_node_id": e["to_node_id"],
                "relation_type": e["relation_type"],
                "weight": float(e["weight"]) if e["weight"] is not None else 0.0,
            }
            for e in edge_rows
            if e["from_node_id"] in node_ids and e["to_node_id"] in node_ids
        ] + [
            {
                "id": -int(e["rn"]),
                "from_node_id": e["from_node_id"],
                "to_node_id": e["to_node_id"],
                "relation_type": "contains",
                "weight": 1.0,
            }
            for e in structure_rows
            if e["from_node_id"] in node_ids and e["to_node_id"] in node_ids
        ],
    }


@router.post("/wiki/rebuild")
async def rebuild_wiki(_: dict = Depends(require_auth)):
    """Trigger a full wiki rebuild job."""
    job = await jobs.enqueue_job(
        "rebuild_wiki", {}, user_id=USER_ID, priority=2,
        idempotency_key=f"rebuild_wiki:{USER_ID}",
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.get("/wiki/status")
async def wiki_status():
    """Return .md file counts per wiki subdirectory. No auth required."""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki"
    counts = {}
    for subdir in ("articles", "entities", "summaries", "indices"):
        d = wiki_dir / subdir
        counts[subdir] = len(list(d.glob("*.md"))) if d.exists() else 0
    return {
        "synced_count": sum(counts.values()),
        "counts": counts,
        "index_exists": (wiki_dir / "index.md").exists(),
    }


@router.get("/jobs")
async def list_background_jobs(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_auth),
):
    return {"jobs": await jobs.list_jobs(USER_ID, status=status, limit=limit)}


@router.get("/jobs/{job_id}")
async def get_background_job(job_id: str, _: dict = Depends(require_auth)):
    job = await jobs.get_job(job_id, USER_ID)
    if not job:
        raise HTTPException(404, "job 不存在")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_background_job(job_id: str, _: dict = Depends(require_auth)):
    job = await jobs.cancel_job(job_id, USER_ID)
    if not job:
        raise HTTPException(404, "job 不存在")
    return job


@router.post("/jobs/{job_id}/retry")
async def retry_background_job(job_id: str, _: dict = Depends(require_auth)):
    job = await jobs.retry_job(job_id, USER_ID)
    if not job:
        raise HTTPException(404, "job 不存在")
    return job


@router.post("/maintenance/run")
async def trigger_maintenance(_: dict = Depends(require_auth)):
    """Trigger KB maintenance (orphan detection, edge repair) as a background job."""
    job = await jobs.enqueue_job(
        "run_maintenance", {}, user_id=USER_ID, priority=1,
        idempotency_key=f"run_maintenance:{USER_ID}",
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.post("/maintenance/rebuild_from_raw")
async def trigger_rebuild_from_raw(body: RebuildFromRawRequest | None = None, _: dict = Depends(require_auth)):
    """Rebuild KB from source_items manifest. Supports filter/dry-run/resume."""
    payload = body.dict(exclude_none=True) if body else {}
    key_parts = [
        "rebuild_from_raw", USER_ID,
        payload.get("source_id") or "",
        payload.get("source_type") or "",
        payload.get("status") or "",
        payload.get("since") or "",
        payload.get("until") or "",
        "dry" if payload.get("dry_run") else "run",
        "resume" if payload.get("resume") else "full",
    ]
    job = await jobs.enqueue_job(
        "rebuild_from_raw", payload, user_id=USER_ID, priority=1,
        idempotency_key=":".join(key_parts),
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}

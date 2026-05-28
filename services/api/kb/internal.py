import hashlib
import json
import secrets
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

import config_loader
import database
import entity_insights
import index_structure
import jobs
import object_nodes
import prompt_loader
from auth import require_auth, require_auth_or_service_token
from kb.common import USER_DATA_DIR, USER_ID, _is_visible_edge, _vector_literal
from kb.retrieval import _embed_text, claude_client
from kb.wiki import _wiki_file_path, write_wiki_index, write_wiki_node

RAW_CAP_BYTES = 512 * 1024 * 1024  # 512 MB

router = APIRouter(prefix="/api/kb", tags=["KB Internal"])

def _make_node_id(
    object_type: str,
    raw_ref: dict,
    user_id: str,
    canonical_name: str | None,
    summary_of: str | None = None,
    perspective_label: str | None = None,
    perspective_instruction: str | None = None,
) -> str:
    """Return a deterministic ID for source-backed nodes and entities; random otherwise."""
    prefix = object_type[:3] if object_type else "nod"
    raw_path = (raw_ref or {}).get("path")
    if raw_path:
        h = hashlib.sha256(raw_path.encode()).hexdigest()[:16]
        return f"{prefix}_{h}"
    raw_url = (raw_ref or {}).get("url")
    if raw_url:
        h = hashlib.sha256(raw_url.encode()).hexdigest()[:16]
        return f"{prefix}_{h}"
    if object_type == "summary" and summary_of:
        h = hashlib.sha256(
            f"{user_id}:{summary_of}:{perspective_label or ''}:{perspective_instruction or ''}".encode()
        ).hexdigest()[:16]
        return f"sum_{h}"
    if object_type == "entity" and canonical_name:
        h = hashlib.sha256(f"{user_id}:{canonical_name}".encode()).hexdigest()[:16]
        return f"ent_{h}"
    return f"{prefix}_{secrets.token_hex(6)}"


def _summary_perspective(
    label: str | None,
    instruction: str | None,
    legacy_perspective: str | None = None,
) -> tuple[str, str, bool]:
    label = (label or legacy_perspective or "").strip()
    instruction = (instruction or legacy_perspective or "").strip()
    is_default = not label and not instruction
    return label or "default", instruction or "默认摘要", is_default


# ── 入库 ──────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    user_id: str = "default"
    title: str | None = None
    abstract: str
    embedding: list[float]
    source_type: str
    source_id: str
    raw_ref: dict[str, Any] = {}
    tags: list[str] = []
    object_type: str = "article"
    source_node_ids: list[str] = []
    summary_of: str | None = None
    canonical_name: str | None = None
    aliases: list[str] = []
    perspective: str | None = None
    perspective_label: str | None = None
    perspective_instruction: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    captured_at: datetime | None = None
    effective_at: datetime | None = None
    source_item_id: str | None = None
    parent_index_id: str | None = None   # if set, adds an index_children row
    doc_kind: str | None = None          # 显式覆盖；未给时由 ingest() 沿 cascade 链推导
    embedding_model: str | None = None   # 生成该 embedding 时所用模型名（用于后续 drift 检测）


class RebuildFromRawRequest(BaseModel):
    source_id: str | None = None
    source_type: str | None = None
    status: str | None = None
    since: str | None = None
    until: str | None = None
    dry_run: bool = False
    resume: bool = False


@router.post("/ingest")
async def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """内容入库唯一写入入口（ingestion-worker 调用，无需认证）。"""
    # Dedup: skip if a node with the same file path already exists
    raw_path = (body.raw_ref or {}).get("path")
    if raw_path:
        existing = await database.database.fetch_one(
            """
            SELECT n.id
            FROM knowledge_nodes n
            JOIN article_nodes an ON an.node_id = n.id
            WHERE n.user_id = :uid AND an.raw_ref->>'path' = :path
            """,
            {"uid": body.user_id, "path": raw_path},
        )
        if existing:
            return {"id": existing["id"], "duplicate": True}
    raw_url = (body.raw_ref or {}).get("url")
    if raw_url:
        existing = await database.database.fetch_one(
            """
            SELECT n.id
            FROM knowledge_nodes n
            JOIN article_nodes an ON an.node_id = n.id
            WHERE n.user_id = :uid AND an.raw_ref->>'url' = :url
            """,
            {"uid": body.user_id, "url": raw_url},
        )
        if existing:
            return {"id": existing["id"], "duplicate": True}

    # Dedup for entities: skip if canonical_name already exists
    # canonical_name 已不在 knowledge_nodes，改读 entity_nodes
    if body.object_type == "entity" and body.canonical_name:
        existing_ent = await database.database.fetch_one(
            """
            SELECT n.id
            FROM knowledge_nodes n
            JOIN entity_nodes en ON en.node_id = n.id
            WHERE n.user_id = :uid AND n.object_type = 'entity'
              AND en.canonical_name = :name
            """,
            {"uid": body.user_id, "name": body.canonical_name},
        )
        if existing_ent:
            return {"id": existing_ent["id"], "duplicate": True}

    embedding_literal = _vector_literal(body.embedding)
    perspective_label = None
    perspective_instruction = None
    is_default = False
    body_embedding_literal = None
    perspective_embedding_literal = None

    # doc_kind cascade：显式提供 > source_items.doc_kind > sources.default_doc_kind > config.doc_kind.default
    doc_kind = (body.doc_kind or "").strip() or None
    if not doc_kind and body.source_item_id:
        si_row = await database.database.fetch_one(
            "SELECT doc_kind FROM source_items WHERE id = :id",
            {"id": body.source_item_id},
        )
        if si_row and si_row["doc_kind"]:
            doc_kind = si_row["doc_kind"]
    if not doc_kind and body.source_id:
        s_row = await database.database.fetch_one(
            "SELECT default_doc_kind FROM sources WHERE id = :id",
            {"id": body.source_id},
        )
        if s_row and s_row["default_doc_kind"]:
            doc_kind = s_row["default_doc_kind"]
    if not doc_kind:
        doc_kind = config_loader.get("doc_kind.default", "other")
    # 仅接受合法枚举；非法值降级为 default 而非 400（ingestion 不应被 prompt 噪声打断）
    allowed_kinds = set(config_loader.get("doc_kind.values", []) or [])
    if allowed_kinds and doc_kind not in allowed_kinds:
        doc_kind = config_loader.get("doc_kind.default", "other")

    # embedding_model：显式 > 当前 config 中的默认（记录当时所用模型，便于将来 drift 检测）
    embedding_model = body.embedding_model or config_loader.get(
        "embedding.model", "text-embedding-3-small"
    )
    if body.object_type == "summary":
        perspective_label, perspective_instruction, is_default = _summary_perspective(
            body.perspective_label,
            body.perspective_instruction,
            body.perspective,
        )
        body_embedding_literal = embedding_literal
        perspective_embedding = await _embed_text(f"{perspective_label}\n{perspective_instruction}")
        perspective_embedding_literal = _vector_literal(perspective_embedding)
    node_id = _make_node_id(
        body.object_type,
        body.raw_ref or {},
        body.user_id,
        body.canonical_name,
        body.summary_of,
        perspective_label,
        perspective_instruction,
    )
    if body.object_type == "summary" and body.summary_of:
        existing_summary = await database.database.fetch_one(
            "SELECT id FROM knowledge_nodes WHERE user_id = :uid AND id = :id",
            {"uid": body.user_id, "id": node_id},
        )
        if existing_summary:
            return {"id": existing_summary["id"], "duplicate": True}

    captured_at = body.captured_at or datetime.now(timezone.utc)
    published_at = body.effective_at or body.source_published_at or captured_at

    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, abstract, embedding, source_id,
           tags, object_type, published_at, doc_kind, embedding_model)
        VALUES
          (:id, :user_id, :title, :abstract, '{embedding_literal}'::vector,
           :source_id, :tags,
           :object_type, :published_at, :doc_kind, :embedding_model)
        """,
        {
            "id": node_id,
            "user_id": body.user_id,
            "title": body.title,
            "abstract": body.abstract,
            "source_id": body.source_id,
            "tags": body.tags,
            "object_type": body.object_type,
            "published_at": published_at,
            "doc_kind": doc_kind,
            "embedding_model": embedding_model,
        },
    )
    await object_nodes.upsert_object_node(
        node_id,
        body.object_type,
        {
            "source_item_id": body.source_item_id,
            "raw_ref": body.raw_ref,
            "source_type": body.source_type,
            "source_published_at": body.source_published_at,
            "source_updated_at": body.source_updated_at,
            "captured_at": captured_at,
            "effective_at": body.effective_at,
            "tags": body.tags,
            "summary_of": body.summary_of,
            "perspective_label": perspective_label,
            "perspective_instruction": perspective_instruction,
            "body": body.abstract,
            "body_embedding_literal": body_embedding_literal,
            "perspective_embedding_literal": perspective_embedding_literal,
            "is_default": is_default,
            "source": {"source_node_ids": body.source_node_ids},
            "canonical_name": body.canonical_name,
            "aliases": body.aliases,
            "description": body.abstract,
        },
    )
    if body.parent_index_id:
        await index_structure.add_child(
            body.parent_index_id,
            node_id,
            user_id=body.user_id,
            child_role="chapter" if body.source_type == "book_chapter" else "member",
        )

    background_tasks.add_task(build_similar_edges_and_wiki, node_id, body.user_id)
    if body.raw_ref:
        background_tasks.add_task(trim_raw_files, body.user_id)
    return {"id": node_id}


async def build_similar_edges(node_id: str, user_id: str):
    """找 cosine 相似度超过阈值的节点，建 similar_to 边。"""
    limit = config_loader.get("retrieval.similar_to_limit", 20)
    threshold = config_loader.get("retrieval.similar_to_threshold", 0.75)
    async with database.database.connection() as conn:
        raw = await conn.raw_connection.fetch(
            f"""
            SELECT id,
                   1 - (embedding <=> (SELECT embedding FROM knowledge_nodes WHERE id = $1)) AS similarity
            FROM knowledge_nodes
            WHERE id != $1
              AND user_id = $2
              AND embedding IS NOT NULL
            ORDER BY embedding <=> (SELECT embedding FROM knowledge_nodes WHERE id = $1)
            LIMIT {limit}
            """,
            node_id, user_id,
        )

    for r in raw:
        sim = float(r["similarity"])
        if sim < threshold:
            break
        await database.database.execute(
            """
            INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
            VALUES (:from_id, :to_id, 'similar_to', :weight, 'auto_semantic')
            ON CONFLICT DO NOTHING
            """,
            {"from_id": node_id, "to_id": r["id"], "weight": sim},
        )


async def build_similar_edges_and_wiki(node_id: str, user_id: str):
    """先建相似边，再写 wiki 文件。"""
    await build_similar_edges(node_id, user_id)
    await write_wiki_node(node_id, user_id)


def trim_raw_files(user_id: str) -> None:
    """若 raw/ 目录超过 RAW_CAP_BYTES，从最旧文件开始删除直到低于上限。"""
    raw_dir = USER_DATA_DIR / user_id / "raw"
    if not raw_dir.exists():
        return
    files = sorted(
        [f for f in raw_dir.rglob("*") if f.is_file()],
        key=lambda f: f.stat().st_mtime,
    )
    total = sum(f.stat().st_size for f in files)
    for f in files:
        if total <= RAW_CAP_BYTES:
            break
        total -= f.stat().st_size
        f.unlink()



@router.post("/wiki/rebuild")
async def rebuild_wiki(_: dict = Depends(require_auth)):
    """触发全量重建 wiki/nodes/*.md 及 wiki/index.md，需要认证。"""
    job = await jobs.enqueue_job(
        "rebuild_wiki",
        {},
        user_id=USER_ID,
        priority=2,
        idempotency_key=f"rebuild_wiki:{USER_ID}",
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.get("/wiki/status")
async def wiki_status():
    """返回 wiki 目录中各子目录的 .md 文件数量，无需认证。"""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki"
    counts = {}
    for subdir in ("articles", "entities", "summaries", "indices"):
        d = wiki_dir / subdir
        counts[subdir] = len(list(d.glob("*.md"))) if d.exists() else 0
    index_path = wiki_dir / "index.md"
    return {
        "synced_count": sum(counts.values()),
        "counts": counts,
        "index_exists": index_path.exists(),
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


# ── 语义搜索 ───────────────────────────────────────────────────────────────────

async def _embed_query(text: str) -> list[float]:
    return await _embed_text(text)


async def _hyde_embed_query(text: str) -> list[float]:
    """Embed query text, optionally via HyDE (Hypothetical Document Embeddings).

    If retrieval.use_hyde is enabled, asks Claude to generate a short hypothetical
    knowledge-base abstract for the topic, then embeds that instead of the raw query.
    Falls back to direct embedding on any error.
    """
    if not config_loader.get("retrieval.use_hyde", True):
        return await _embed_query(text)
    try:
        hypo = await claude_client.messages.create(
            model=config_loader.get("models.hyde_abstract", "claude-haiku-4-5-20251001"),
            max_tokens=config_loader.get("llm_output_tokens.hyde_abstract", 200),
            messages=[{"role": "user", "content": prompt_loader.fill("hyde_abstract", topic=text)}],
        )
        hypo_text = getattr(hypo.content[0], "text", "").strip()
        if hypo_text:
            return await _embed_query(hypo_text)
    except Exception:
        pass
    return await _embed_query(text)


@router.get("/search")
async def search(
    q: str,
    limit: int = Query(10, ge=1, le=50),
    tags: str | None = None,
    type: str | None = None,          # article | entity | summary
    _: dict = Depends(require_auth_or_service_token),
):
    """语义搜索（RAG 核心调用）。"""
    embedding = await _embed_query(q)
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
                   s.perspective_label AS perspective_label,
                   s.perspective_instruction AS perspective_instruction,
                   s.is_default AS is_default,
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


# ── 单节点详情 ─────────────────────────────────────────────────────────────────

@router.get("/node/{node_id}")
async def get_node(node_id: str, _: dict = Depends(require_auth_or_service_token)):
    """获取单个节点及其所有边。"""
    node = await object_nodes.fetch_node_with_object_fields(node_id)
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
    for key in (
        "created_at",
        "updated_at",
        "ingested_at",
        "published_at",
        "source_published_at",
        "source_updated_at",
        "captured_at",
        "effective_at",
    ):
        if node.get(key):
            node[key] = node[key].isoformat()

    # Read wiki body from correct subdirectory
    wiki_body = ""
    object_type = node.get("object_type") or "article"
    wiki_file = _wiki_file_path(node.get("user_id") or USER_ID, node_id, object_type)
    if wiki_file.exists():
        raw_wiki = wiki_file.read_text(encoding="utf-8")
        parts = raw_wiki.split("---", 2)
        if len(parts) >= 3:
            body_section = parts[2].strip()
            if "\n## 関連節点\n" in body_section:
                body_section = body_section[: body_section.index("\n## 関連節点\n")].strip()
            if "\n## 关联节点\n" in body_section:
                body_section = body_section[: body_section.index("\n## 关联节点\n")].strip()
            lines = body_section.split("\n", 2)
            if len(lines) >= 3:
                wiki_body = lines[2].strip()
            elif len(lines) < 2:
                wiki_body = body_section

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


# ── 多视角摘要生成 ────────────────────────────────────────────────────────────

class CreateSummaryRequest(BaseModel):
    perspective: str | None = None
    perspective_label: str | None = None
    perspective_instruction: str | None = None


class ReviseSummaryRequest(BaseModel):
    instruction: str
    perspective_label: str | None = None
    perspective_instruction: str | None = None


class CreateIndexRequest(BaseModel):
    title: str
    description: str | None = None
    rollup_instruction: str | None = None
    tags: list[str] = []


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


class UpdateNodeMetadataRequest(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    published_at: datetime | None = None
    doc_kind: str | None = None


class UpdateEntityRequest(BaseModel):
    canonical_name: str | None = None
    aliases: list[str] | None = None
    entity_type: str | None = None


class MergeEntitiesRequest(BaseModel):
    source_id: str  # 被合并方（成为 tombstone）
    target_id: str  # 保留方


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
    node = await object_nodes.fetch_node_with_object_fields(index_id)
    if not node or node.get("object_type") != "index":
        raise HTTPException(404, "index 不存在")
    node.pop("embedding", None)
    node.pop("body_embedding", None)
    node.pop("perspective_embedding", None)
    for key in ("created_at", "updated_at", "ingested_at"):
        if node.get(key):
            node[key] = node[key].isoformat()
    return node


@router.post("/indices")
async def create_index(body: CreateIndexRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "title 不能为空")
    description = (body.description or "").strip()
    embedding = await _embed_text("\n".join([title, description]).strip() or title)
    embedding_literal = _vector_literal(embedding)
    index_id = f"idx_{secrets.token_hex(6)}"
    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, abstract, embedding, source_id,
           tags, object_type)
        VALUES
          (:id, :user_id, :title, :abstract, '{embedding_literal}'::vector,
           :source_id, :tags, 'index')
        """,
        {
            "id": index_id,
            "user_id": USER_ID,
            "title": title,
            "abstract": description,
            "source_id": index_id,
            "tags": body.tags,
        },
    )
    await object_nodes.upsert_object_node(
        index_id,
        "index",
        {
            "description": description,
            "rollup_instruction": body.rollup_instruction,
            "abstract_stale": False,
        },
    )
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    background_tasks.add_task(write_wiki_index, USER_ID)
    return await _get_index_or_404(index_id)


@router.get("/indices/{index_id}")
async def get_index(index_id: str):
    return await _get_index_or_404(index_id)


@router.patch("/indices/{index_id}")
async def update_index(index_id: str, body: UpdateIndexRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    current = await _get_index_or_404(index_id)
    title = body.title.strip() if body.title is not None else current.get("title")
    if not title:
        raise HTTPException(400, "title 不能为空")
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
        SET title = :title,
            abstract = :description,
            tags = :tags,
            updated_at = NOW()
        WHERE id = :id AND user_id = :user_id AND object_type = 'index'
        """,
        {
            "id": index_id,
            "user_id": USER_ID,
            "title": title,
            "description": description or "",
            "tags": tags,
        },
    )
    await object_nodes.upsert_object_node(
        index_id,
        "index",
        {
            "description": description,
            "rollup_instruction": rollup_instruction,
            "abstract_stale": abstract_stale or bool(current.get("abstract_stale")),
        },
    )
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    background_tasks.add_task(write_wiki_index, USER_ID)
    return await _get_index_or_404(index_id)


@router.get("/indices/{index_id}/children")
async def get_index_children(index_id: str):
    await _get_index_or_404(index_id)
    return {"children": _serialize_index_rows(await index_structure.get_children(index_id, USER_ID))}


@router.post("/indices/{index_id}/children")
async def add_index_child(index_id: str, body: AddIndexChildRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        child = await index_structure.add_child(
            index_id,
            body.child_id,
            user_id=USER_ID,
            position=body.position,
            child_role=body.child_role,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    return child


@router.delete("/indices/{index_id}/children/{child_id}")
async def remove_index_child(index_id: str, child_id: str, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        deleted = await index_structure.remove_child(index_id, child_id, USER_ID)
    except ValueError as e:
        raise HTTPException(404, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    return {"ok": True, "deleted": deleted}


@router.patch("/indices/{index_id}/children/order")
async def reorder_index_children(index_id: str, body: ReorderIndexChildrenRequest, background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    try:
        await index_structure.reorder_children(index_id, body.child_ids, USER_ID)
    except ValueError as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(write_wiki_node, index_id, USER_ID)
    return {"ok": True, "children": _serialize_index_rows(await index_structure.get_children(index_id, USER_ID))}


@router.get("/objects/{object_id}/parents")
async def get_object_parents(object_id: str):
    return {"parents": _serialize_index_rows(await index_structure.get_parents(object_id, USER_ID))}


@router.get("/objects/{object_id}/ancestors")
async def get_object_ancestors(object_id: str, _: dict = Depends(require_auth_or_service_token)):
    return {"ancestors": _serialize_index_rows(await index_structure.get_ancestors(object_id, USER_ID))}


@router.get("/indices/{index_id}/descendants")
async def get_index_descendants(index_id: str):
    await _get_index_or_404(index_id)
    return {"descendants": _serialize_index_rows(await index_structure.get_descendants(index_id, USER_ID))}


@router.post("/indices/{index_id}/rollup")
async def rollup_index(index_id: str, _: dict = Depends(require_auth)):
    await _get_index_or_404(index_id)
    job = await jobs.enqueue_job(
        "aggregate_index_abstract",
        {"index_id": index_id, "only_stale": False},
        user_id=USER_ID,
        provider="anthropic",
        model=config_loader.get("models.index_summary", "claude-haiku-4-5-20251001"),
        priority=10,
        idempotency_key=f"aggregate_index_abstract:{USER_ID}:{index_id}",
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


async def generate_summary_job(
    node_id: str,
    perspective_label_input: str | None,
    perspective_instruction_input: str | None,
    user_id: str = USER_ID,
) -> dict[str, Any]:
    source = await database.database.fetch_one(
        """
        SELECT id, user_id, title, abstract, object_type
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
        perspective_label_input,
        perspective_instruction_input,
        None,
    )

    prompt_perspective_instruction = (
        f"\n\n请从以下视角撰写摘要：{perspective_instruction}" if not is_default else ""
    )
    prompt = prompt_loader.fill(
        "summary_gen",
        title=source["title"] or node_id,
        abstract=source["abstract"] or "",
        body=(source["abstract"] or "")[:3000],
        perspective_instruction=prompt_perspective_instruction,
    )

    message = await claude_client.messages.create(
        model=config_loader.get("models.summary_gen", "claude-haiku-4-5-20251001"),
        max_tokens=config_loader.get("llm_output_tokens.summary_gen", 1024),
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
           tags, object_type)
        VALUES
          (:id, :user_id, :title, :abstract, '{body_embedding_literal}'::vector,
           :source_id, :tags,
           :object_type)
        """,
        {
            "id": summary_id,
            "user_id": user_id,
            "title": summary_title,
            "abstract": summary_content,
            "source_id": node_id,
            "tags": [],
            "object_type": "summary",
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
    # summarizes 关系由 summary_nodes.summary_of FK 表达，不再写 knowledge_edges
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


@router.post("/nodes/{node_id}/create_summary")
async def create_summary(
    node_id: str,
    body: CreateSummaryRequest,
):
    """为 article 或 index 节点生成新的摘要（可指定视角），创建 summarizes 边。"""
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
        body.perspective_label,
        body.perspective_instruction,
        body.perspective,
    )
    key = f"generate_summary:{user_id}:{node_id}:{perspective_label}:{perspective_instruction}"
    job = await jobs.enqueue_job(
        "generate_summary",
        {
            "node_id": node_id,
            "perspective_label": perspective_label,
            "perspective_instruction": perspective_instruction,
        },
        user_id=user_id,
        provider="anthropic",
        model=config_loader.get("models.summary_gen", "claude-haiku-4-5-20251001"),
        priority=5,
        idempotency_key=key,
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


async def revise_summary_job(
    node_id: str,
    instruction: str,
    perspective_label_input: str | None,
    perspective_instruction_input: str | None,
    user_id: str = USER_ID,
) -> dict[str, Any]:
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

    source = None
    if summary["summary_of"]:
        source = await database.database.fetch_one(
            "SELECT id, title, abstract, object_type FROM knowledge_nodes WHERE id = :id",
            {"id": summary["summary_of"]},
        )

    source_context = ""
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
        model=config_loader.get("models.summary_gen", "claude-haiku-4-5-20251001"),
        max_tokens=config_loader.get("llm_output_tokens.summary_gen", 1024),
        messages=[{"role": "user", "content": prompt}],
    )
    revised_content = getattr(message.content[0], "text", "").strip()

    body_embedding = await _embed_text(revised_content)
    body_embedding_literal = _vector_literal(body_embedding)
    perspective_changed = perspective_label_input is not None or perspective_instruction_input is not None
    perspective_label = summary["perspective_label"] or None
    perspective_instruction = summary["perspective_instruction"] or None
    is_default = bool(summary["is_default"])
    params = {"id": node_id, "abstract": revised_content}
    perspective_embedding_literal = None
    if perspective_changed:
        perspective_label, perspective_instruction, is_default = _summary_perspective(
            perspective_label_input,
            perspective_instruction_input,
            None,
        )
        perspective_embedding = await _embed_text(f"{perspective_label}\n{perspective_instruction}")
        perspective_embedding_literal = _vector_literal(perspective_embedding)

    await database.database.execute(
        f"""
        UPDATE knowledge_nodes
        SET abstract = :abstract,
            embedding = '{body_embedding_literal}'::vector,
            updated_at = NOW()
        WHERE id = :id
        """,
        params,
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


@router.post("/nodes/{node_id}/revise_summary")
async def revise_summary(
    node_id: str,
    body: ReviseSummaryRequest,
):
    """按用户指令重写 summary 正文，并同步更新 embedding 与 wiki 导出。"""
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(400, "修改指令不能为空")

    summary = await database.database.fetch_one(
        """
        SELECT id, user_id, object_type
        FROM knowledge_nodes WHERE id = :id
        """,
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
        model=config_loader.get("models.summary_gen", "claude-haiku-4-5-20251001"),
        priority=5,
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.delete("/summaries/{summary_id}")
async def delete_summary(summary_id: str, _: dict = Depends(require_auth)):
    """删除 summary 节点（wiki 文件 + summary_nodes + knowledge_nodes）。"""
    row = await database.database.fetch_one(
        "SELECT user_id, object_type FROM knowledge_nodes WHERE id = :id",
        {"id": summary_id},
    )
    if not row:
        raise HTTPException(404, "节点不存在")
    if row["object_type"] != "summary":
        raise HTTPException(400, "只能删除 summary 节点")

    uid = row["user_id"] or USER_ID
    wiki_file = _wiki_file_path(uid, summary_id, "summary")
    if wiki_file.exists():
        wiki_file.unlink()

    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": summary_id},
    )
    await database.database.execute(
        "DELETE FROM summary_nodes WHERE node_id = :id",
        {"id": summary_id},
    )
    await database.database.execute(
        "DELETE FROM knowledge_nodes WHERE id = :id",
        {"id": summary_id},
    )
    return {"ok": True}


# ── 节点删除 ──────────────────────────────────────────────────────────────────

@router.delete("/nodes/{node_id}")
async def delete_node(node_id: str, _: dict = Depends(require_auth)):
    """删除节点：wiki 文件 + 边 + DB 记录（raw 文件独立管理，不随节点删除）。"""
    row = await database.database.fetch_one(
        "SELECT user_id, object_type FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    if not row:
        raise HTTPException(404, "节点不存在")

    uid = row["user_id"] or USER_ID
    object_type = row["object_type"] or "article"

    # Delete wiki file from correct subdirectory
    wiki_file = _wiki_file_path(uid, node_id, object_type)
    if wiki_file.exists():
        wiki_file.unlink()

    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": node_id},
    )
    await database.database.execute(
        "DELETE FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )

    return {"ok": True}


@router.post("/nodes/{node_id}/archive")
async def archive_node(node_id: str, _: dict = Depends(require_auth)):
    """软删除 article 节点（article_nodes.status = 'archived'）。"""
    row = await database.database.fetch_one(
        "SELECT object_type FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    if not row:
        raise HTTPException(404, "节点不存在")
    if row["object_type"] != "article":
        raise HTTPException(400, "archive 仅适用于 article 节点")

    await database.database.execute(
        "UPDATE article_nodes SET status = 'archived' WHERE node_id = :id",
        {"id": node_id},
    )
    return {"ok": True}


@router.patch("/nodes/{node_id}/metadata")
async def update_node_metadata(
    node_id: str,
    body: UpdateNodeMetadataRequest,
    _: dict = Depends(require_auth),
):
    """修改节点元数据（title / tags / published_at / doc_kind）。"""
    row = await database.database.fetch_one(
        "SELECT object_type FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    if not row:
        raise HTTPException(404, "节点不存在")

    if body.doc_kind is not None:
        allowed = config_loader.get("doc_kind.values", [])
        if body.doc_kind not in allowed:
            raise HTTPException(400, f"无效的 doc_kind；可选值：{', '.join(allowed)}")

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

    return {"ok": True}


# ── 图谱查询（BFS） ────────────────────────────────────────────────────────────

@router.get("/graph")
async def get_graph(root: str, depth: int = Query(2, ge=1, le=3)):
    """从 root 节点 BFS 扩展，返回节点集合 + 边集合，无需认证。"""
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
            edge_key = ed["id"]
            if edge_key not in visited_edges:
                visited_edges.add(edge_key)
                edges_out.append(ed)
            neighbor = ed["to_node_id"] if ed["from_node_id"] == node_id else ed["from_node_id"]
            if neighbor not in visited_nodes:
                queue.append((neighbor, current_depth + 1))

    return {"nodes": nodes_out, "edges": edges_out}


# ── 节点列表（分页） ─────────────────────────────────────────────────────────────

@router.get("/nodes")
async def list_nodes(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tags: str | None = None,
    q: str | None = None,
    type: str | None = None,
    source_id: str | None = None,
):
    """分页列出节点，支持文本搜索、标签、类型和来源过滤，无需认证。"""
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
                   n.source_id, n.doc_kind,
                   s.name AS source_name
            FROM knowledge_nodes n
            LEFT JOIN article_nodes an ON an.node_id = n.id
            LEFT JOIN sources s ON s.id = n.source_id
            WHERE {where}
            ORDER BY n.created_at DESC
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
                "source_id": r["source_id"],
                "source_name": r["source_name"],
                "doc_kind": r["doc_kind"],
            }
            for r in rows
        ],
        "total": total,
    }


# ── 全量图谱（D3 用） ─────────────────────────────────────────────────────────

@router.get("/graph/all")
async def get_full_graph(
    limit: int = Query(300, ge=1, le=500),
    type: str | None = None,
):
    """返回全量节点和边（含 degree、object_type），用于 D3 力导向图，无需认证。"""
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
        edge_rows = await conn.raw_connection.fetch(
            """
            SELECT id, from_node_id, to_node_id, relation_type, weight
            FROM knowledge_edges
            WHERE relation_type NOT IN ('extends', 'background_of', 'supports', 'contradicts', 'part_of')
            LIMIT 1000
            """
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


# ── Entity Candidate 端点 ─────────────────────────────────────────────────────

@router.post("/entity_candidates/analyze_context")
async def entity_analyze_context(body: dict):
    """
    给 ingestion-worker 提供入库分析上下文：
    - nearby_entities：用 article embedding 找最近的 20 个已有 entity 节点
    - top_candidates：mention_count 数前 20 的候选池条目
    - popular_tags：库中出现频次前 50 的 tags（tag 收敛机制，引导 LLM 优先复用）
    """
    embedding = body.get("embedding", [])
    if not embedding:
        return {"nearby_entities": [], "top_candidates": [], "popular_tags": []}

    embedding_literal = "[" + ",".join(repr(x) for x in embedding) + "]"
    nearby_limit = config_loader.get("ingestion.context_nearby_entities", 20)
    candidate_limit = config_loader.get("ingestion.context_top_candidates", 20)
    tags_limit = config_loader.get("ingestion.context_popular_tags", 50)

    async with database.database.connection() as conn:
        entity_rows = await conn.raw_connection.fetch(
            f"""
            SELECT n.id, n.title, COALESCE(en.canonical_name, n.title) AS canonical_name
            FROM knowledge_nodes n
            LEFT JOIN entity_nodes en ON en.node_id = n.id
            WHERE n.user_id = $1 AND n.object_type = 'entity' AND n.embedding IS NOT NULL
            ORDER BY n.embedding <=> '{embedding_literal}'::vector
            LIMIT {nearby_limit}
            """,
            USER_ID,
        )
        candidate_rows = await conn.raw_connection.fetch(
            f"""
            SELECT id, canonical_name, aliases, mention_count, max_salience
            FROM entity_candidates
            WHERE user_id = $1 AND promoted_entity_id IS NULL
            ORDER BY mention_count DESC, max_salience DESC
            LIMIT {candidate_limit}
            """,
            USER_ID,
        )
        tag_rows = await conn.raw_connection.fetch(
            f"""
            SELECT tag, COUNT(*) AS freq
            FROM knowledge_nodes, unnest(tags) AS tag
            WHERE user_id = $1 AND tags IS NOT NULL
            GROUP BY tag
            ORDER BY freq DESC, tag ASC
            LIMIT {tags_limit}
            """,
            USER_ID,
        )

    return {
        "nearby_entities": [
            {"id": r["id"], "title": r["title"] or r["canonical_name"] or r["id"]}
            for r in entity_rows
        ],
        "top_candidates": [
            {"id": r["id"], "canonical_name": r["canonical_name"], "mention_count": r["mention_count"]}
            for r in candidate_rows
        ],
        "popular_tags": [
            {"tag": r["tag"], "freq": int(r["freq"])} for r in tag_rows
        ],
    }


class EntityCandidateItem(BaseModel):
    name: str
    aliases: list[str] = []
    salience: float
    matches_existing_entity_id: str | None = None
    summary_hint: str = ""


class ProcessCandidatesRequest(BaseModel):
    article_id: str
    entities: list[EntityCandidateItem]


@router.post("/entity_candidates/process")
async def process_entity_candidates(body: ProcessCandidatesRequest):
    """
    处理 ingestion-worker 提交的 entity 候选列表：
    - matches_existing → 追加 source_node_ids
    - 新词 → upsert entity_candidates（递增计数器 + source_article_ids 数组追加）
    - 检查晋升条件，返回新晋升的候选
    """
    matched_existing: list[str] = []
    promoted: list[dict] = []

    for ent in body.entities:
        if ent.matches_existing_entity_id:
            await entity_insights.upsert_fact_from_mention(
                ent.matches_existing_entity_id,
                body.article_id,
                summary_hint=ent.summary_hint,
                salience=ent.salience,
                user_id=USER_ID,
            )
            matched_existing.append(ent.matches_existing_entity_id)
            continue

        # Upsert into entity_candidates（计数器 + source_article_ids 数组，无 JSONB）
        existing_cand = await database.database.fetch_one(
            """
            SELECT id, source_article_ids
            FROM entity_candidates
            WHERE user_id = :uid AND canonical_name = :name
            """,
            {"uid": USER_ID, "name": ent.name},
        )

        if existing_cand:
            cand_id = existing_cand["id"]
            existing_article_ids = list(existing_cand["source_article_ids"] or [])
            # 仅当该 article_id 未出现过时才递增计数 + 追加 id（保持幂等）
            if body.article_id not in existing_article_ids:
                await database.database.execute(
                    """
                    UPDATE entity_candidates
                    SET source_article_ids = array_append(
                            COALESCE(source_article_ids, '{}'), :article_id),
                        mention_count = COALESCE(mention_count, 0) + 1,
                        max_salience = GREATEST(COALESCE(max_salience, 0), :salience),
                        aliases = (
                            SELECT array(SELECT DISTINCT unnest(aliases || CAST(:new_aliases AS text[])))
                        ),
                        updated_at = NOW()
                    WHERE id = :cid
                    """,
                    {
                        "cid": cand_id,
                        "article_id": body.article_id,
                        "new_aliases": ent.aliases,
                        "salience": float(ent.salience),
                    },
                )
        else:
            await database.database.execute(
                """
                INSERT INTO entity_candidates
                  (user_id, canonical_name, aliases, source_article_ids,
                   mention_count, max_salience)
                VALUES (:uid, :name, :aliases, ARRAY[:article_id]::text[], 1, :salience)
                """,
                {
                    "uid": USER_ID,
                    "name": ent.name,
                    "aliases": ent.aliases,
                    "article_id": body.article_id,
                    "salience": float(ent.salience),
                },
            )
            cand_id_row = await database.database.fetch_one(
                "SELECT id FROM entity_candidates WHERE user_id = :uid AND canonical_name = :name",
                {"uid": USER_ID, "name": ent.name},
            )
            cand_id = cand_id_row["id"] if cand_id_row else None

        if cand_id is None:
            continue
        cand_row = await database.database.fetch_one(
            """
            SELECT id, canonical_name, aliases, source_article_ids,
                   promoted_entity_id, mention_count, max_salience
            FROM entity_candidates WHERE id = :cid
            """,
            {"cid": cand_id},
        )
        if not cand_row:
            continue
        if cand_row["promoted_entity_id"]:
            continue
        mention_count = int(cand_row["mention_count"] or 0)
        max_salience = float(cand_row["max_salience"] or 0)
        source_article_ids = list(cand_row["source_article_ids"] or [])

        should_promote = (
            max_salience >= config_loader.get("entity.promotion_max_salience", 0.9)
            or (max_salience >= config_loader.get("entity.promotion_salience", 0.7)
                and mention_count >= config_loader.get("entity.promotion_salience_mentions", 2))
            or mention_count >= config_loader.get("entity.promotion_min_mentions", 3)
        )
        if should_promote:
            cand_aliases = list(cand_row["aliases"]) if cand_row["aliases"] else []
            promoted.append({
                "candidate_id": cand_row["id"],
                "canonical_name": cand_row["canonical_name"],
                "aliases": cand_aliases,
                "source_article_ids": source_article_ids,
                "summary_hint": ent.summary_hint,
            })

    return {
        "matched_existing": matched_existing,
        "promoted": promoted,
    }


@router.post("/entities/{entity_id}/backfill_wikilinks")
async def backfill_entity_wikilinks(entity_id: str):
    """新 entity 晋升后，回扫所有 article 正文注入 wikilink。由 ingestion-worker 调用。"""
    from maintenance import backfill_wikilinks_for_entity
    result = await backfill_wikilinks_for_entity(entity_id, USER_ID)
    return result


async def _materialize_candidate_facts(candidate_id: int, entity_node_id: str) -> dict:
    """
    新 entity 晋升后回填 entity_facts。

    历史 mentions JSONB 保存了 per-article 的 (salience, summary_hint)；移除后
    候选侧只保留聚合 max_salience。这里用 max_salience 作为每篇文章的回填权重，
    summary_hint 不再回填（ingestion 时如有匹配会通过 process_entity_candidates
    主路径写入 entity_facts，此函数是兜底）。
    """
    cand = await database.database.fetch_one(
        """
        SELECT canonical_name, source_article_ids, max_salience
        FROM entity_candidates
        WHERE id = :cid
        """,
        {"cid": candidate_id},
    )
    if not cand:
        return {"facts_inserted": 0}
    article_ids = list(cand["source_article_ids"] or [])
    fallback_salience = float(cand["max_salience"] or 0.5) or 0.5
    inserted = 0
    for article_id in article_ids:
        if not article_id:
            continue
        created = await entity_insights.upsert_fact_from_mention(
            entity_node_id,
            article_id,
            canonical_name=cand["canonical_name"],
            summary_hint=None,
            salience=fallback_salience,
            user_id=USER_ID,
        )
        if created:
            inserted += 1
    return {"facts_inserted": inserted}


@router.post("/entity_candidates/{candidate_id}/mark_promoted")
async def mark_candidate_promoted(candidate_id: int, body: dict):
    """ingestion-worker 生成 entity 页后调用，标记候选已晋升。"""
    entity_node_id = body.get("entity_node_id")
    if not entity_node_id:
        raise HTTPException(400, "entity_node_id 必填")
    await database.database.execute(
        "UPDATE entity_candidates SET promoted_entity_id = :eid WHERE id = :cid",
        {"eid": entity_node_id, "cid": candidate_id},
    )
    facts_result = await _materialize_candidate_facts(candidate_id, entity_node_id)
    await entity_insights.refresh_entity_profile(entity_node_id)
    return {"ok": True, **facts_result}


@router.get("/entities/{entity_id}/facts")
async def list_entity_facts(entity_id: str, limit: int = Query(50, ge=1, le=200)):
    rows = await database.database.fetch_all(
        """
        SELECT ef.id, ef.entity_id, ef.article_id, ef.source_item_id,
               ef.fact_text, ef.fact_time, ef.source_published_at,
               ef.evidence_span, ef.confidence, ef.created_at,
               n.title AS article_title
        FROM entity_facts ef
        LEFT JOIN knowledge_nodes n ON n.id = ef.article_id
        WHERE ef.entity_id = :entity_id
        ORDER BY ef.fact_time DESC NULLS LAST, ef.created_at DESC
        LIMIT :limit
        """,
        {"entity_id": entity_id, "limit": limit},
    )
    return [
        {
            "id": r["id"],
            "entity_id": r["entity_id"],
            "article_id": r["article_id"],
            "article_title": r["article_title"],
            "source_item_id": r["source_item_id"],
            "fact_text": r["fact_text"],
            "fact_time": r["fact_time"].isoformat() if r["fact_time"] else None,
            "source_published_at": (
                r["source_published_at"].isoformat() if r["source_published_at"] else None
            ),
            "evidence_span": r["evidence_span"],
            "confidence": float(r["confidence"] or 0),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


@router.get("/entities/{entity_id}/timeline")
async def get_entity_timeline(entity_id: str, limit: int = Query(50, ge=1, le=200)):
    """返回 entity 的 facts 时间序列。

    历史上还会返回 entity_profiles 的 timeline_summary / status / refreshed_at，
    该表已删除——entity 描述统一回 nodes.abstract，按需可由
    POST /api/kb/entities/{id}/regenerate 触发更新。
    """
    facts = await list_entity_facts(entity_id, limit)
    abstract_row = await database.database.fetch_one(
        "SELECT abstract, updated_at FROM knowledge_nodes WHERE id = :id",
        {"id": entity_id},
    )
    return {
        "entity_id": entity_id,
        "abstract": abstract_row["abstract"] if abstract_row else "",
        "abstract_updated_at": (
            abstract_row["updated_at"].isoformat()
            if abstract_row and abstract_row["updated_at"] else None
        ),
        "facts": facts,
    }


@router.get("/entities/{entity_id}/related")
async def get_related_entities(entity_id: str, limit: int = Query(20, ge=1, le=100)):
    rows = await database.database.fetch_all(
        """
        SELECT eps.*,
               other.id AS related_entity_id,
               COALESCE(en.canonical_name, other.title) AS related_title
        FROM entity_pair_signals eps
        JOIN knowledge_nodes other ON other.id = CASE
          WHEN eps.entity_a_id = :entity_id THEN eps.entity_b_id
          ELSE eps.entity_a_id
        END
        LEFT JOIN entity_nodes en ON en.node_id = other.id
        WHERE eps.entity_a_id = :entity_id OR eps.entity_b_id = :entity_id
        ORDER BY eps.relatedness_score DESC, eps.co_occurrence_count DESC
        LIMIT :limit
        """,
        {"entity_id": entity_id, "limit": limit},
    )
    return [
        {
            "entity_id": r["related_entity_id"],
            "title": r["related_title"],
            "relatedness_score": float(r["relatedness_score"] or 0),
            "co_occurrence_count": int(r["co_occurrence_count"] or 0),
            "co_occurrence_score": float(r["co_occurrence_score"] or 0),
            "embedding_similarity": float(r["embedding_similarity"] or 0),
            "graph_proximity_score": float(r["graph_proximity_score"] or 0),
            "temporal_score": float(r["temporal_score"] or 0),
            "explanation": r["explanation"],
            "source_article_ids": list(r["source_article_ids"] or []),
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.post("/entities/{entity_id}/regenerate")
async def regenerate_entity_profile(entity_id: str, _: dict = Depends(require_auth)):
    result = await entity_insights.refresh_entity_profile(entity_id)
    if not result.get("refreshed"):
        raise HTTPException(404, "entity 不存在")
    return result


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: str,
    body: UpdateEntityRequest,
    _: dict = Depends(require_auth),
):
    """修改 entity 的 canonical_name / aliases / entity_type。"""
    row = await database.database.fetch_one(
        "SELECT object_type FROM knowledge_nodes WHERE id = :id",
        {"id": entity_id},
    )
    if not row or row["object_type"] != "entity":
        raise HTTPException(404, "entity 不存在")

    entity_updates: list[str] = []
    params: dict[str, Any] = {"id": entity_id}
    if body.canonical_name is not None:
        entity_updates.append("canonical_name = :canonical_name")
        params["canonical_name"] = body.canonical_name.strip()
    if body.aliases is not None:
        entity_updates.append("aliases = :aliases")
        params["aliases"] = body.aliases
    if body.entity_type is not None:
        entity_updates.append("entity_type = :entity_type")
        params["entity_type"] = body.entity_type.strip()

    if entity_updates:
        await database.database.execute(
            f"UPDATE entity_nodes SET {', '.join(entity_updates)} WHERE node_id = :id",
            params,
        )

    if body.canonical_name is not None:
        await database.database.execute(
            "UPDATE knowledge_nodes SET title = :name, updated_at = NOW() WHERE id = :id",
            {"name": body.canonical_name.strip(), "id": entity_id},
        )

    return {"ok": True}


@router.post("/entities/merge")
async def merge_entities(body: MergeEntitiesRequest, _: dict = Depends(require_auth)):
    """将 source entity 合并入 target entity。
    mentions 边和 entity_facts 转移到 target，source 保留为 tombstone（merged_into 指向 target）。
    """
    source_id, target_id = body.source_id, body.target_id
    if source_id == target_id:
        raise HTTPException(400, "source 和 target 不能相同")

    for eid in (source_id, target_id):
        row = await database.database.fetch_one(
            "SELECT object_type FROM knowledge_nodes WHERE id = :id",
            {"id": eid},
        )
        if not row or row["object_type"] != "entity":
            raise HTTPException(404, f"entity 不存在：{eid}")

    # Transfer non-conflicting edges where source is the from-node
    await database.database.execute(
        """
        UPDATE knowledge_edges SET from_node_id = :target
        WHERE from_node_id = :source
          AND NOT EXISTS (
            SELECT 1 FROM knowledge_edges e2
            WHERE e2.from_node_id = :target
              AND e2.to_node_id = knowledge_edges.to_node_id
              AND e2.relation_type = knowledge_edges.relation_type
          )
        """,
        {"source": source_id, "target": target_id},
    )
    # Transfer non-conflicting edges where source is the to-node
    await database.database.execute(
        """
        UPDATE knowledge_edges SET to_node_id = :target
        WHERE to_node_id = :source
          AND NOT EXISTS (
            SELECT 1 FROM knowledge_edges e2
            WHERE e2.to_node_id = :target
              AND e2.from_node_id = knowledge_edges.from_node_id
              AND e2.relation_type = knowledge_edges.relation_type
          )
        """,
        {"source": source_id, "target": target_id},
    )
    # Delete remaining duplicate edges still referencing source
    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :source OR to_node_id = :source",
        {"source": source_id},
    )

    # Transfer entity_facts
    await database.database.execute(
        "UPDATE entity_facts SET entity_id = :target WHERE entity_id = :source",
        {"source": source_id, "target": target_id},
    )

    # Mark source as merged (tombstone)
    await database.database.execute(
        "UPDATE entity_nodes SET merged_into = :target WHERE node_id = :source",
        {"source": source_id, "target": target_id},
    )

    return {"ok": True, "source_id": source_id, "target_id": target_id}


@router.delete("/entities/{entity_id}")
async def delete_entity(entity_id: str, _: dict = Depends(require_auth)):
    """硬删除 entity（级联删 entity_facts / edges / entity_nodes / knowledge_nodes）。"""
    row = await database.database.fetch_one(
        "SELECT user_id, object_type FROM knowledge_nodes WHERE id = :id",
        {"id": entity_id},
    )
    if not row or row["object_type"] != "entity":
        raise HTTPException(404, "entity 不存在")

    uid = row["user_id"] or USER_ID
    wiki_file = _wiki_file_path(uid, entity_id, "entity")
    if wiki_file.exists():
        wiki_file.unlink()

    await database.database.execute(
        "DELETE FROM entity_facts WHERE entity_id = :id",
        {"id": entity_id},
    )
    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": entity_id},
    )
    await database.database.execute(
        "DELETE FROM entity_nodes WHERE node_id = :id",
        {"id": entity_id},
    )
    await database.database.execute(
        "DELETE FROM knowledge_nodes WHERE id = :id",
        {"id": entity_id},
    )
    return {"ok": True}


@router.get("/entity_candidates")
async def list_entity_candidates(_: dict = Depends(require_auth)):
    """列出未晋升的 entity 候选（按 mention_count 排序），供调试用。"""
    async with database.database.connection() as conn:
        rows = await conn.raw_connection.fetch(
            """
            SELECT id, canonical_name, aliases, mention_count, max_salience, updated_at
            FROM entity_candidates
            WHERE user_id = $1 AND promoted_entity_id IS NULL
            ORDER BY mention_count DESC, max_salience DESC
            LIMIT 100
            """,
            USER_ID,
        )
    return [
        {
            "id": r["id"],
            "canonical_name": r["canonical_name"],
            "aliases": list(r["aliases"]) if r["aliases"] else [],
            "mention_count": int(r["mention_count"] or 0),
            "max_salience": float(r["max_salience"] or 0),
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


# ── 维护触发（空壳） ───────────────────────────────────────────────────────────

@router.post("/maintenance/run")
async def trigger_maintenance(_: dict = Depends(require_auth)):
    """触发知识库维护（孤岛检测 + 补边 + 矛盾发现），后台运行。"""
    job = await jobs.enqueue_job(
        "run_maintenance",
        {},
        user_id=USER_ID,
        priority=1,
        idempotency_key=f"run_maintenance:{USER_ID}",
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}


@router.post("/maintenance/rebuild_from_raw")
async def trigger_rebuild_from_raw(
    body: RebuildFromRawRequest | None = None,
    _: dict = Depends(require_auth),
):
    """从 source_items manifest 重建知识库。后台运行；支持 filter/dry-run/resume。"""
    payload = body.dict(exclude_none=True) if body else {}
    key_parts = [
        "rebuild_from_raw",
        USER_ID,
        payload.get("source_id") or "",
        payload.get("source_type") or "",
        payload.get("status") or "",
        payload.get("since") or "",
        payload.get("until") or "",
        "dry" if payload.get("dry_run") else "run",
        "resume" if payload.get("resume") else "full",
    ]
    job = await jobs.enqueue_job(
        "rebuild_from_raw",
        payload,
        user_id=USER_ID,
        priority=1,
        idempotency_key=":".join(key_parts),
    )
    return {"status": job["status"], "job_id": job["id"], "job": job}

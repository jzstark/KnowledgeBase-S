import json
import os
import pathlib
import secrets
from collections import deque
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from openai import AsyncOpenAI
from pydantic import BaseModel

import config_loader
import database
from auth import require_auth

USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
RAW_CAP_BYTES = 512 * 1024 * 1024  # 512 MB

router = APIRouter(prefix="/api/kb", tags=["kb"])

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

USER_ID = "default"


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
    is_primary: bool = True
    object_type: str = "article"
    source_node_ids: list[str] = []
    summary_of: str | None = None
    canonical_name: str | None = None
    aliases: list[str] = []


@router.post("/ingest")
async def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """内容入库唯一写入入口（ingestion-worker 调用，无需认证）。"""
    # Dedup: skip if a node with the same file path already exists
    raw_path = (body.raw_ref or {}).get("path")
    if raw_path:
        existing = await database.database.fetch_one(
            "SELECT id FROM knowledge_nodes WHERE user_id = :uid AND raw_ref->>'path' = :path",
            {"uid": body.user_id, "path": raw_path},
        )
        if existing:
            return {"id": existing["id"], "duplicate": True}

    prefix = body.object_type[:3] if body.object_type else "nod"
    node_id = f"{prefix}_{secrets.token_hex(6)}"
    embedding_literal = "[" + ",".join(repr(x) for x in body.embedding) + "]"

    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, abstract, embedding, source_type, source_id, raw_ref,
           tags, is_primary, object_type, source_node_ids, summary_of, canonical_name, aliases)
        VALUES
          (:id, :user_id, :title, :abstract, '{embedding_literal}'::vector,
           :source_type, :source_id, :raw_ref, :tags, :is_primary,
           :object_type, :source_node_ids, :summary_of, :canonical_name, :aliases)
        """,
        {
            "id": node_id,
            "user_id": body.user_id,
            "title": body.title,
            "abstract": body.abstract,
            "source_type": body.source_type,
            "source_id": body.source_id,
            "raw_ref": database.jsonb(body.raw_ref),
            "tags": body.tags,
            "is_primary": body.is_primary,
            "object_type": body.object_type,
            "source_node_ids": body.source_node_ids,
            "summary_of": body.summary_of,
            "canonical_name": body.canonical_name,
            "aliases": body.aliases,
        },
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


# ── Obsidian Wiki 同步 ─────────────────────────────────────────────────────────

def _wiki_subdir(object_type: str) -> str:
    """Map object_type to wiki subdirectory name."""
    return {"article": "articles", "entity": "entities", "summary": "summaries"}.get(object_type, "articles")


def _wiki_file_path(user_id: str, node_id: str, object_type: str) -> pathlib.Path:
    subdir = _wiki_subdir(object_type)
    return USER_DATA_DIR / user_id / "wiki" / subdir / f"{node_id}.md"


async def write_wiki_node(node_id: str, user_id: str) -> None:
    """将单个知识节点写入对应 wiki 子目录（articles/entities/summaries）。"""
    row = await database.database.fetch_one(
        """
        SELECT id, title, abstract, source_type, raw_ref, tags, object_type,
               source_node_ids, summary_of, canonical_name, aliases, created_at, updated_at
        FROM knowledge_nodes WHERE id = :id
        """,
        {"id": node_id},
    )
    if not row:
        return

    node = dict(row)
    object_type = node.get("object_type") or "article"

    edges = await database.database.fetch_all(
        "SELECT from_node_id, to_node_id, relation_type FROM knowledge_edges "
        "WHERE from_node_id = :id OR to_node_id = :id",
        {"id": node_id},
    )

    tags: list[str] = list(node["tags"]) if node["tags"] else []
    created_at = node["created_at"].isoformat() if node["created_at"] else ""
    updated_at = node["updated_at"].isoformat() if node.get("updated_at") else created_at

    raw_ref = node["raw_ref"]
    if isinstance(raw_ref, str):
        raw_ref = json.loads(raw_ref)
    raw_ref_str = ""
    if raw_ref:
        if raw_ref.get("type") == "file":
            raw_ref_str = raw_ref.get("path", "")
        elif raw_ref.get("type") == "url":
            raw_ref_str = raw_ref.get("url", "")

    # Collect wikilinks from edge list
    wikilinks = []
    relations = []
    for e in edges:
        ed = dict(e)
        other = ed["to_node_id"] if ed["from_node_id"] == node_id else ed["from_node_id"]
        if ed["relation_type"] == "wikilink":
            wikilinks.append(other)
        else:
            relations.append({"id": other, "type": ed["relation_type"]})

    tags_yaml = "[" + ", ".join(tags) + "]"
    wikilinks_yaml = "[" + ", ".join(wikilinks) + "]"
    source_node_ids = list(node.get("source_node_ids") or [])
    sources_yaml = "[" + ", ".join(source_node_ids) + "]"
    aliases = list(node.get("aliases") or [])
    aliases_yaml = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"

    relations_yaml = ""
    if relations:
        relations_yaml = "\nrelations:"
        for rel in relations:
            relations_yaml += f"\n  - id: {rel['id']}\n    type: {rel['type']}"

    title = node["title"] or node_id
    wiki_file = _wiki_file_path(user_id, node_id, object_type)
    wiki_file.parent.mkdir(parents=True, exist_ok=True)

    # Preserve existing body text; update only frontmatter and relations section
    existing_body = node["abstract"] or ""
    if wiki_file.exists():
        raw_content = wiki_file.read_text(encoding="utf-8")
        parts = raw_content.split("---", 2)
        if len(parts) >= 3:
            body_section = parts[2].strip()
            if "\n## 关联节点\n" in body_section:
                body_section = body_section[:body_section.index("\n## 关联节点\n")].strip()
            lines = body_section.split("\n", 2)
            if len(lines) >= 3:
                existing_body = lines[2].strip()
            elif len(lines) == 2:
                existing_body = ""
            else:
                existing_body = body_section

    # Build type-specific frontmatter extras
    extra_fm = f"\nsource_type: {node['source_type'] or ''}\nraw_ref: {raw_ref_str}"
    if object_type == "entity":
        extra_fm = f"\ncanonical_name: {node.get('canonical_name') or title}\naliases: {aliases_yaml}\nsources: {sources_yaml}"
    elif object_type == "summary":
        extra_fm = f"\nsummary_of: {node.get('summary_of') or ''}\nsources: {sources_yaml}"

    content = f"""---
id: {node_id}
type: {object_type}
title: "{title}"
tags: {tags_yaml}
wikilinks: {wikilinks_yaml}{extra_fm}
created_at: {created_at}
updated_at: {updated_at}{relations_yaml}
---

# {title}

{existing_body}
"""
    if relations:
        content += "\n## 关联节点\n"
        for rel in relations:
            content += f"- [[{rel['id']}]] · {rel['type']}\n"

    wiki_file.write_text(content, encoding="utf-8")


async def write_wiki_index(user_id: str) -> None:
    """重新生成 wiki/index.md（按类型和日期分组）。"""
    rows = await database.database.fetch_all(
        """
        SELECT id, title, source_type, tags, object_type, created_at
        FROM knowledge_nodes WHERE user_id = :user_id
        ORDER BY object_type, created_at DESC
        """,
        {"user_id": user_id},
    )

    lines = ["# 知识库索引\n\n> 自动生成，请勿手动修改。\n\n"]
    lines.append(f"共 **{len(rows)}** 个对象。\n")

    sections: dict[str, list] = {"article": [], "entity": [], "summary": []}
    for r in rows:
        r = dict(r)
        ot = r.get("object_type") or "article"
        sections.setdefault(ot, []).append(r)

    section_labels = {"article": "文章", "entity": "实体", "summary": "摘要"}
    for ot, items in sections.items():
        if not items:
            continue
        label = section_labels.get(ot, ot)
        subdir = _wiki_subdir(ot)
        lines.append(f"\n## {label}（{len(items)}）\n\n")
        for r in items:
            title = r["title"] or r["id"]
            tags_str = " ".join(f"#{t}" for t in (r["tags"] or []))
            lines.append(f"- [[{subdir}/{r['id']}|{title}]] {tags_str}\n")

    wiki_dir = USER_DATA_DIR / user_id / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "index.md").write_text("".join(lines), encoding="utf-8")


async def _do_rebuild_wiki(user_id: str) -> dict:
    rows = await database.database.fetch_all(
        "SELECT id FROM knowledge_nodes WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    for r in rows:
        await write_wiki_node(r["id"], user_id)
    await write_wiki_index(user_id)
    return {"rebuilt": len(rows)}


@router.post("/wiki/rebuild")
async def rebuild_wiki(background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    """触发全量重建 wiki/nodes/*.md 及 wiki/index.md，需要认证。"""
    background_tasks.add_task(_do_rebuild_wiki, USER_ID)
    return {"status": "rebuilding"}


@router.get("/wiki/status")
async def wiki_status():
    """返回 wiki 目录中各子目录的 .md 文件数量，无需认证。"""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki"
    counts = {}
    for subdir in ("articles", "entities", "summaries"):
        d = wiki_dir / subdir
        counts[subdir] = len(list(d.glob("*.md"))) if d.exists() else 0
    index_path = wiki_dir / "index.md"
    return {
        "synced_count": sum(counts.values()),
        "counts": counts,
        "index_exists": index_path.exists(),
    }


# ── 语义搜索 ───────────────────────────────────────────────────────────────────

async def _embed_query(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model=config_loader.get("embedding.model", "text-embedding-3-small"),
        input=text[:config_loader.get("embedding.max_chars", 8000)],
        dimensions=config_loader.get("embedding.dimensions", 1536),
    )
    return resp.data[0].embedding


@router.get("/search")
async def search(
    q: str,
    limit: int = Query(10, ge=1, le=50),
    tags: str | None = None,
    type: str | None = None,          # article | entity | summary
):
    """语义搜索（RAG 核心调用），无需认证。"""
    embedding = await _embed_query(q)
    embedding_literal = "[" + ",".join(repr(x) for x in embedding) + "]"

    tag_list: list[str] = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    conditions = ["embedding IS NOT NULL"]
    extra_params: list = []

    if tag_list:
        placeholders = ", ".join(f"${i+2}" for i in range(len(tag_list)))
        conditions.append(f"tags && ARRAY[{placeholders}]::text[]")
        extra_params.extend(tag_list)

    if type:
        ti = len(extra_params) + 2
        conditions.append(f"object_type = ${ti}")
        extra_params.append(type)

    where = " AND ".join(conditions)
    limit_idx = len(extra_params) + 1

    async with database.database.connection() as conn:
        sql = f"""
            SELECT id, user_id, title, abstract, source_type, tags, object_type, created_at,
                   1 - (embedding <=> '{embedding_literal}'::vector) AS score
            FROM knowledge_nodes
            WHERE {where}
            ORDER BY embedding <=> '{embedding_literal}'::vector
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
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ── 单节点详情 ─────────────────────────────────────────────────────────────────

@router.get("/node/{node_id}")
async def get_node(node_id: str):
    """获取单个节点及其所有边，无需认证。"""
    row = await database.database.fetch_one(
        "SELECT * FROM knowledge_nodes WHERE id = :id", {"id": node_id}
    )
    if not row:
        raise HTTPException(404, "节点不存在")

    edges = await database.database.fetch_all(
        "SELECT * FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": node_id},
    )

    node = dict(row)
    node.pop("embedding", None)
    if node.get("raw_ref") and isinstance(node["raw_ref"], str):
        node["raw_ref"] = json.loads(node["raw_ref"])
    if node.get("created_at"):
        node["created_at"] = node["created_at"].isoformat()
    if node.get("updated_at"):
        node["updated_at"] = node["updated_at"].isoformat()

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
        "edges": [dict(e) for e in edges],
    }


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
            "SELECT id, title, abstract, source_type, tags, object_type, created_at FROM knowledge_nodes WHERE id = :id",
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
        for e in edge_rows:
            ed = dict(e)
            if ed["id"] not in visited_edges:
                visited_edges.add(ed["id"])
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
):
    """分页列出节点，支持文本搜索、标签和类型过滤，无需认证。"""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    conditions = ["user_id = $1"]
    params: list = [USER_ID]

    if tag_list:
        placeholders = ", ".join(f"${i + 2}" for i in range(len(tag_list)))
        conditions.append(f"tags && ARRAY[{placeholders}]::text[]")
        params.extend(tag_list)

    if q and q.strip():
        qi = len(params) + 1
        conditions.append(f"(title ILIKE ${qi} OR abstract ILIKE ${qi})")
        params.append(f"%{q.strip()}%")

    if type:
        ti = len(params) + 1
        conditions.append(f"object_type = ${ti}")
        params.append(type)

    where = " AND ".join(conditions)
    limit_idx = len(params) + 1
    offset_idx = len(params) + 2

    async with database.database.connection() as conn:
        total = await conn.raw_connection.fetchval(
            f"SELECT COUNT(*) FROM knowledge_nodes WHERE {where}", *params
        )
        rows = await conn.raw_connection.fetch(
            f"""
            SELECT id, title, abstract, source_type, tags, object_type, created_at
            FROM knowledge_nodes
            WHERE {where}
            ORDER BY created_at DESC
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
            SELECT n.id, n.title, n.source_type, n.tags, n.object_type,
                   COUNT(e.id)::int AS degree
            FROM knowledge_nodes n
            LEFT JOIN knowledge_edges e ON e.from_node_id = n.id OR e.to_node_id = n.id
            WHERE n.user_id = $1 {type_filter}
            GROUP BY n.id
            ORDER BY n.created_at DESC
            LIMIT ${limit_idx}
            """,
            *params,
        )
        edge_rows = await conn.raw_connection.fetch(
            "SELECT id, from_node_id, to_node_id, relation_type, weight FROM knowledge_edges LIMIT 1000"
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
        ],
    }


# ── 写作偏好记忆 ───────────────────────────────────────────────────────────────

class MemoryFeedback(BaseModel):
    template_name: str
    rule: str
    rule_type: str   # 'style'|'structure'|'content'|'tone'


@router.post("/memory/feedback")
async def add_memory(body: MemoryFeedback):
    """写入或更新偏好规则。同一 (template_name, rule) 已存在则 confidence +0.15。"""
    existing = await database.database.fetch_one(
        """
        SELECT id, confidence, count FROM writing_memory
        WHERE user_id = :user_id AND template_name = :template_name AND rule = :rule
        """,
        {"user_id": USER_ID, "template_name": body.template_name, "rule": body.rule},
    )

    if existing:
        new_confidence = min(1.0, float(existing["confidence"]) + 0.15)
        await database.database.execute(
            """
            UPDATE writing_memory
            SET confidence = :confidence, count = count + 1, updated_at = NOW()
            WHERE id = :id
            """,
            {"confidence": new_confidence, "id": existing["id"]},
        )
        return {"updated": True, "confidence": new_confidence}
    else:
        await database.database.execute(
            """
            INSERT INTO writing_memory (user_id, template_name, rule, rule_type)
            VALUES (:user_id, :template_name, :rule, :rule_type)
            """,
            {
                "user_id": USER_ID,
                "template_name": body.template_name,
                "rule": body.rule,
                "rule_type": body.rule_type,
            },
        )
        return {"updated": False, "confidence": 0.5}


@router.get("/memory")
async def get_memory(
    template_name: str | None = None,
    min_confidence: float = 0.0,
):
    """读取偏好规则，按置信度降序。无需认证（供 worker 调用）。"""
    if template_name:
        rows = await database.database.fetch_all(
            """
            SELECT * FROM writing_memory
            WHERE user_id = :user_id AND template_name = :template_name
              AND confidence >= :min_confidence
            ORDER BY confidence DESC
            """,
            {"user_id": USER_ID, "template_name": template_name, "min_confidence": min_confidence},
        )
    else:
        rows = await database.database.fetch_all(
            """
            SELECT * FROM writing_memory
            WHERE user_id = :user_id AND confidence >= :min_confidence
            ORDER BY confidence DESC
            """,
            {"user_id": USER_ID, "min_confidence": min_confidence},
        )
    return [dict(r) for r in rows]


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: int, _: dict = Depends(require_auth)):
    await database.database.execute(
        "DELETE FROM writing_memory WHERE id = :id AND user_id = :user_id",
        {"id": memory_id, "user_id": USER_ID},
    )
    return {"ok": True}


# ── Entity Candidate 端点 ─────────────────────────────────────────────────────

@router.post("/entity_candidates/analyze_context")
async def entity_analyze_context(body: dict):
    """
    给 ingestion-worker 提供 entity 上下文：
    - 用 article embedding 找最近的 20 个已有 entity 节点（title + id）
    - 返回 mention 数前 20 的候选池条目（canonical_name）
    """
    embedding = body.get("embedding", [])
    if not embedding:
        return {"nearby_entities": [], "top_candidates": []}

    embedding_literal = "[" + ",".join(repr(x) for x in embedding) + "]"

    async with database.database.connection() as conn:
        entity_rows = await conn.raw_connection.fetch(
            f"""
            SELECT id, title, canonical_name
            FROM knowledge_nodes
            WHERE user_id = $1 AND object_type = 'entity' AND embedding IS NOT NULL
            ORDER BY embedding <=> '{embedding_literal}'::vector
            LIMIT 20
            """,
            USER_ID,
        )
        candidate_rows = await conn.raw_connection.fetch(
            """
            SELECT id, canonical_name, aliases,
                   jsonb_array_length(mentions) AS mention_count
            FROM entity_candidates
            WHERE user_id = $1 AND promoted_entity_id IS NULL
            ORDER BY jsonb_array_length(mentions) DESC
            LIMIT 20
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
    - 新词 → upsert entity_candidates（累加 mention）
    - 检查晋升条件，返回新晋升的候选
    """
    import datetime as dt

    now_iso = dt.datetime.utcnow().isoformat()
    matched_existing: list[str] = []
    promoted: list[dict] = []

    for ent in body.entities:
        if ent.matches_existing_entity_id:
            # Append article to existing entity's source list
            await database.database.execute(
                """
                UPDATE knowledge_nodes
                SET source_node_ids = array_append(
                    COALESCE(source_node_ids, '{}'),
                    :article_id
                ),
                updated_at = NOW()
                WHERE id = :eid
                  AND NOT (:article_id = ANY(COALESCE(source_node_ids, '{}')))
                """,
                {"eid": ent.matches_existing_entity_id, "article_id": body.article_id},
            )
            matched_existing.append(ent.matches_existing_entity_id)
            continue

        # Upsert into entity_candidates
        mention_entry = database.jsonb(
            {"article_id": body.article_id, "salience": ent.salience, "seen_at": now_iso}
        )
        existing_cand = await database.database.fetch_one(
            "SELECT id, mentions FROM entity_candidates WHERE user_id = :uid AND canonical_name = :name",
            {"uid": USER_ID, "name": ent.name},
        )

        if existing_cand:
            cand_id = existing_cand["id"]
            mentions = existing_cand["mentions"]
            if isinstance(mentions, str):
                import json as _json
                mentions = _json.loads(mentions)
            # avoid duplicate article
            if not any(m.get("article_id") == body.article_id for m in mentions):
                await database.database.execute(
                    """
                    UPDATE entity_candidates
                    SET mentions = mentions || CAST(:entry AS jsonb),
                        aliases = (
                            SELECT array(SELECT DISTINCT unnest(aliases || CAST(:new_aliases AS text[])))
                        ),
                        updated_at = NOW()
                    WHERE id = :cid
                    """,
                    {
                        "cid": cand_id,
                        "entry": f"[{mention_entry}]",
                        "new_aliases": ent.aliases,
                    },
                )
        else:
            # Insert new candidate (embedding computed lazily; skip here for now)
            await database.database.execute(
                """
                INSERT INTO entity_candidates (user_id, canonical_name, aliases, mentions)
                VALUES (:uid, :name, :aliases, CAST(:mentions AS jsonb))
                """,
                {
                    "uid": USER_ID,
                    "name": ent.name,
                    "aliases": ent.aliases,
                    "mentions": f"[{mention_entry}]",
                },
            )
            cand_id_row = await database.database.fetch_one(
                "SELECT id FROM entity_candidates WHERE user_id = :uid AND canonical_name = :name",
                {"uid": USER_ID, "name": ent.name},
            )
            cand_id = cand_id_row["id"] if cand_id_row else None

        # Check promotion threshold
        if cand_id is None:
            continue
        cand_row = await database.database.fetch_one(
            "SELECT id, canonical_name, aliases, mentions, promoted_entity_id FROM entity_candidates WHERE id = :cid",
            {"cid": cand_id},
        )
        if not cand_row:
            continue
        if cand_row["promoted_entity_id"]:
            continue  # already promoted, skip
        mentions_raw = cand_row["mentions"]
        if isinstance(mentions_raw, str):
            import json as _json
            mentions_raw = _json.loads(mentions_raw)
        mention_count = len(mentions_raw)
        max_salience = max((m.get("salience", 0) for m in mentions_raw), default=0)
        source_article_ids = [m["article_id"] for m in mentions_raw]

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
    return {"ok": True}


@router.get("/entity_candidates")
async def list_entity_candidates(_: dict = Depends(require_auth)):
    """列出未晋升的 entity 候选（按 mention 数排序），供调试用。"""
    async with database.database.connection() as conn:
        rows = await conn.raw_connection.fetch(
            """
            SELECT id, canonical_name, aliases,
                   jsonb_array_length(mentions) AS mention_count,
                   updated_at
            FROM entity_candidates
            WHERE user_id = $1 AND promoted_entity_id IS NULL
            ORDER BY jsonb_array_length(mentions) DESC
            LIMIT 100
            """,
            USER_ID,
        )
    return [
        {
            "id": r["id"],
            "canonical_name": r["canonical_name"],
            "aliases": list(r["aliases"]) if r["aliases"] else [],
            "mention_count": r["mention_count"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


# ── 维护触发（空壳） ───────────────────────────────────────────────────────────

@router.post("/maintenance/run")
async def trigger_maintenance(background_tasks: BackgroundTasks, _: dict = Depends(require_auth)):
    """触发知识库维护（孤岛检测 + 补边 + 矛盾发现），后台运行。"""
    from maintenance import run_maintenance
    background_tasks.add_task(run_maintenance, USER_ID)
    return {"status": "triggered"}

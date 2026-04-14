import json
import os
import pathlib
import secrets
from collections import deque
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from openai import AsyncOpenAI
from pydantic import BaseModel

import database
from auth import require_auth

USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))

router = APIRouter(prefix="/api/kb", tags=["kb"])

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

USER_ID = "default"


# ── 入库 ──────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    user_id: str = "default"
    title: str | None = None
    summary: str
    embedding: list[float]
    source_type: str
    source_id: str
    raw_ref: dict[str, Any]
    tags: list[str] = []
    is_primary: bool = True


@router.post("/ingest")
async def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """内容入库唯一写入入口（ingestion-worker 调用，无需认证）。"""
    node_id = f"node_{secrets.token_hex(6)}"
    embedding_literal = "[" + ",".join(repr(x) for x in body.embedding) + "]"

    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, summary, embedding, source_type, source_id, raw_ref, tags, is_primary)
        VALUES
          (:id, :user_id, :title, :summary, '{embedding_literal}'::vector,
           :source_type, :source_id, :raw_ref, :tags, :is_primary)
        """,
        {
            "id": node_id,
            "user_id": body.user_id,
            "title": body.title,
            "summary": body.summary,
            "source_type": body.source_type,
            "source_id": body.source_id,
            "raw_ref": database.jsonb(body.raw_ref),
            "tags": body.tags,
            "is_primary": body.is_primary,
        },
    )
    background_tasks.add_task(build_similar_edges_and_wiki, node_id, body.user_id)
    return {"id": node_id}


async def build_similar_edges(node_id: str, user_id: str):
    """找 cosine 相似度 > 0.75 的节点，建 similar_to 边。"""
    async with database.database.connection() as conn:
        raw = await conn.raw_connection.fetch(
            """
            SELECT id,
                   1 - (embedding <=> (SELECT embedding FROM knowledge_nodes WHERE id = $1)) AS similarity
            FROM knowledge_nodes
            WHERE id != $1
              AND user_id = $2
              AND embedding IS NOT NULL
            ORDER BY embedding <=> (SELECT embedding FROM knowledge_nodes WHERE id = $1)
            LIMIT 20
            """,
            node_id, user_id,
        )

    for r in raw:
        sim = float(r["similarity"])
        if sim < 0.75:
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


# ── Obsidian Wiki 同步 ─────────────────────────────────────────────────────────

async def write_wiki_node(node_id: str, user_id: str) -> None:
    """将单个知识节点写入 wiki/nodes/{node_id}.md（Obsidian 兼容格式）。"""
    row = await database.database.fetch_one(
        """
        SELECT id, title, summary, source_type, raw_ref, tags, created_at
        FROM knowledge_nodes WHERE id = :id
        """,
        {"id": node_id},
    )
    if not row:
        return

    node = dict(row)

    edges = await database.database.fetch_all(
        "SELECT from_node_id, to_node_id, relation_type FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
        {"id": node_id},
    )

    tags: list[str] = list(node["tags"]) if node["tags"] else []
    created_at = node["created_at"].isoformat() if node["created_at"] else ""

    raw_ref = node["raw_ref"]
    if isinstance(raw_ref, str):
        raw_ref = json.loads(raw_ref)
    raw_ref_str = ""
    if raw_ref:
        if raw_ref.get("type") == "file":
            raw_ref_str = raw_ref.get("path", "")
        elif raw_ref.get("type") == "url":
            raw_ref_str = raw_ref.get("url", "")

    relations = []
    for e in edges:
        ed = dict(e)
        other = ed["to_node_id"] if ed["from_node_id"] == node_id else ed["from_node_id"]
        relations.append({"id": other, "type": ed["relation_type"]})

    tags_yaml = "[" + ", ".join(tags) + "]"
    relations_yaml = ""
    if relations:
        relations_yaml = "\nrelations:"
        for rel in relations:
            relations_yaml += f"\n  - id: {rel['id']}\n    type: {rel['type']}"

    title = node["title"] or node_id
    summary = node["summary"] or ""

    content = f"""---
id: {node_id}
source_type: {node["source_type"] or ""}
raw_ref: {raw_ref_str}
tags: {tags_yaml}
created_at: {created_at}{relations_yaml}
---

# {title}

{summary}
"""
    if relations:
        content += "\n## 关联节点\n"
        for rel in relations:
            content += f"- [[{rel['id']}]] · {rel['type']}\n"

    wiki_dir = USER_DATA_DIR / user_id / "wiki" / "nodes"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / f"{node_id}.md").write_text(content, encoding="utf-8")


async def write_wiki_index(user_id: str) -> None:
    """重新生成 wiki/index.md（按日期分组，列出所有节点）。"""
    rows = await database.database.fetch_all(
        """
        SELECT id, title, source_type, tags, created_at
        FROM knowledge_nodes WHERE user_id = :user_id
        ORDER BY created_at DESC
        """,
        {"user_id": user_id},
    )

    lines = ["# 知识库索引\n\n> 自动生成，请勿手动修改。\n\n"]
    lines.append(f"共 **{len(rows)}** 个节点。\n")

    current_date = None
    for r in rows:
        r = dict(r)
        date_str = r["created_at"].strftime("%Y-%m-%d") if r["created_at"] else "未知"
        if date_str != current_date:
            current_date = date_str
            lines.append(f"\n## {date_str}\n\n")
        title = r["title"] or r["id"]
        tags_str = " ".join(f"#{t}" for t in (r["tags"] or []))
        lines.append(f"- [[nodes/{r['id']}|{title}]] {tags_str}\n")

    wiki_dir = USER_DATA_DIR / user_id / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "index.md").write_text("".join(lines), encoding="utf-8")


async def _do_rebuild_wiki(user_id: str) -> dict:
    """重建全部 wiki 文件，返回统计信息。"""
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
    """返回 wiki 目录中已生成的 .md 文件数量，无需认证。"""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "nodes"
    if not wiki_dir.exists():
        return {"synced_count": 0, "index_exists": False}
    md_files = list(wiki_dir.glob("*.md"))
    index_path = USER_DATA_DIR / USER_ID / "wiki" / "index.md"
    return {
        "synced_count": len(md_files),
        "index_exists": index_path.exists(),
    }


# ── 语义搜索 ───────────────────────────────────────────────────────────────────

async def _embed_query(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],
        dimensions=1536,
    )
    return resp.data[0].embedding


@router.get("/search")
async def search(
    q: str,
    limit: int = Query(10, ge=1, le=50),
    tags: str | None = None,          # 逗号分隔，如 "AI,产品"
):
    """语义搜索（RAG 核心调用），无需认证。"""
    embedding = await _embed_query(q)
    embedding_literal = "[" + ",".join(repr(x) for x in embedding) + "]"

    tag_filter = ""
    tag_list: list[str] = []
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    async with database.database.connection() as conn:
        if tag_list:
            placeholders = ", ".join(f"${i+2}" for i in range(len(tag_list)))
            sql = f"""
                SELECT id, user_id, title, summary, source_type, tags, created_at,
                       1 - (embedding <=> '{embedding_literal}'::vector) AS score
                FROM knowledge_nodes
                WHERE embedding IS NOT NULL
                  AND tags && ARRAY[{placeholders}]::text[]
                ORDER BY embedding <=> '{embedding_literal}'::vector
                LIMIT $1
            """
            rows = await conn.raw_connection.fetch(sql, limit, *tag_list)
        else:
            sql = f"""
                SELECT id, user_id, title, summary, source_type, tags, created_at,
                       1 - (embedding <=> '{embedding_literal}'::vector) AS score
                FROM knowledge_nodes
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> '{embedding_literal}'::vector
                LIMIT $1
            """
            rows = await conn.raw_connection.fetch(sql, limit)

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "summary": r["summary"],
            "source_type": r["source_type"],
            "tags": r["tags"],
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
        """
        SELECT * FROM knowledge_edges
        WHERE from_node_id = :id OR to_node_id = :id
        """,
        {"id": node_id},
    )

    node = dict(row)
    node.pop("embedding", None)   # 不返回向量数据（太大）
    if node.get("raw_ref") and isinstance(node["raw_ref"], str):
        node["raw_ref"] = json.loads(node["raw_ref"])
    if node.get("created_at"):
        node["created_at"] = node["created_at"].isoformat()

    return {
        **node,
        "edges": [dict(e) for e in edges],
    }


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
            "SELECT id, title, summary, source_type, tags, created_at FROM knowledge_nodes WHERE id = :id",
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
):
    """分页列出节点，支持文本搜索和标签过滤，无需认证。"""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    conditions = ["user_id = $1"]
    params: list = [USER_ID]

    if tag_list:
        placeholders = ", ".join(f"${i + 2}" for i in range(len(tag_list)))
        conditions.append(f"tags && ARRAY[{placeholders}]::text[]")
        params.extend(tag_list)

    if q and q.strip():
        qi = len(params) + 1
        conditions.append(f"(title ILIKE ${qi} OR summary ILIKE ${qi})")
        params.append(f"%{q.strip()}%")

    where = " AND ".join(conditions)
    limit_idx = len(params) + 1
    offset_idx = len(params) + 2

    async with database.database.connection() as conn:
        total = await conn.raw_connection.fetchval(
            f"SELECT COUNT(*) FROM knowledge_nodes WHERE {where}", *params
        )
        rows = await conn.raw_connection.fetch(
            f"""
            SELECT id, title, summary, source_type, tags, created_at
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
                "summary": r["summary"],
                "source_type": r["source_type"],
                "tags": list(r["tags"]) if r["tags"] else [],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": total,
    }


# ── 全量图谱（D3 用） ─────────────────────────────────────────────────────────

@router.get("/graph/all")
async def get_full_graph(limit: int = Query(300, ge=1, le=500)):
    """返回全量节点和边（含 degree），用于 D3 力导向图，无需认证。"""
    async with database.database.connection() as conn:
        node_rows = await conn.raw_connection.fetch(
            """
            SELECT n.id, n.title, n.source_type, n.tags,
                   COUNT(e.id)::int AS degree
            FROM knowledge_nodes n
            LEFT JOIN knowledge_edges e ON e.from_node_id = n.id OR e.to_node_id = n.id
            WHERE n.user_id = $1
            GROUP BY n.id
            ORDER BY n.created_at DESC
            LIMIT $2
            """,
            USER_ID, limit,
        )
        edge_rows = await conn.raw_connection.fetch(
            "SELECT id, from_node_id, to_node_id, relation_type, weight FROM knowledge_edges LIMIT 600"
        )

    node_ids = {r["id"] for r in node_rows}
    return {
        "nodes": [
            {
                "id": r["id"],
                "title": r["title"],
                "source_type": r["source_type"],
                "tags": list(r["tags"]) if r["tags"] else [],
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


# ── 维护触发（空壳） ───────────────────────────────────────────────────────────

@router.post("/maintenance/run")
async def trigger_maintenance(_: dict = Depends(require_auth)):
    """触发知识库维护（第十二步实现）。"""
    return {"status": "triggered"}

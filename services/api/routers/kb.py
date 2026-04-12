import json
import os
import secrets
from collections import deque
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from openai import AsyncOpenAI
from pydantic import BaseModel

import database
from auth import require_auth

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
    background_tasks.add_task(build_similar_edges, node_id, body.user_id)
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


# ── 写作偏好记忆 ───────────────────────────────────────────────────────────────

class MemoryFeedback(BaseModel):
    template_name: str
    rule: str
    rule_type: str   # 'style'|'structure'|'content'|'tone'


@router.post("/memory/feedback")
async def add_memory(body: MemoryFeedback, _: dict = Depends(require_auth)):
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

import asyncio
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/kb", tags=["kb"])


class IngestRequest(BaseModel):
    user_id: str = "default"
    title: str | None = None
    summary: str
    embedding: list[float]           # 1536 维
    source_type: str
    source_id: str
    raw_ref: dict[str, Any]
    tags: list[str] = []
    is_primary: bool = True


@router.post("/ingest")
async def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """内容入库唯一写入入口（ingestion-worker 调用，无需用户认证）。"""
    node_id = f"node_{secrets.token_hex(6)}"
    # embedding 直接格式化进 SQL（全是浮点数，无注入风险）
    # databases 底层用 SQLAlchemy text()，::vector 会误解析命名参数
    embedding_literal = "[" + ",".join(repr(x) for x in body.embedding) + "]"

    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, summary, embedding, source_type, source_id, raw_ref, tags, is_primary)
        VALUES
          (:id, :user_id, :title, :summary, '{embedding_literal}'::vector, :source_type,
           :source_id, :raw_ref, :tags, :is_primary)
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
    # <=> 运算符让 SQLAlchemy text() 解析异常，改用 asyncpg 原生接口
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
    rows = [{"id": r["id"], "similarity": r["similarity"]} for r in raw]

    for row in rows:
        sim = float(row["similarity"])
        if sim < 0.75:
            break
        await database.database.execute(
            """
            INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
            VALUES (:from_id, :to_id, 'similar_to', :weight, 'auto_semantic')
            ON CONFLICT DO NOTHING
            """,
            {"from_id": node_id, "to_id": row["id"], "weight": sim},
        )

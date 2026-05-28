"""
KB Public — MCP 稳定接口（只读）

挂载于 /api/kb/v1/，由外部 MCP adapter（~/Code/kb-chat/）调用。
接口稳定，变更需前向兼容。

7 个工具端点：
- GET  /search              关键词/语义混合搜索
- GET  /nodes/{id}          节点详情（fetch）
- POST /nodes/batch         批量 fetch
- GET  /nodes/{id}/related  关系导航
- GET  /timeline            时间轴（entity 或 topic 锚点）
- POST /compare             多节点对比（LLM）
- POST /cite                引证查找（两阶段：向量粗筛 → LLM 精确匹配 → 服务端 quote 验证）
- POST /summarize_corpus    语料综述（LLM）
"""
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import config_loader
import database
import object_nodes
import prompt_loader
from auth import require_auth_or_service_token
from kb.common import USER_ID, _is_visible_edge, _vector_literal
from kb.retrieval import _embed_query, claude_client
from kb.wiki import _read_wiki_body

router = APIRouter(tags=["KB Public"])


# ── 通用类型 ──────────────────────────────────────────────────────────────

DocKind = Literal[
    "regulation", "case", "news", "memo", "contract", "analysis", "other"
]
NodeType = Literal["article", "entity", "summary", "index"]
Relation = Literal[
    "mentions", "mentioned_by",
    "summarizes", "summarized_by",
    "contains", "part_of",
]
OutputFormat = Literal["bullet", "prose", "structured"]


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _validate_doc_kinds(values: list[str]) -> list[str]:
    if not values:
        return []
    allowed = set(config_loader.get("doc_kind.values", []) or [])
    invalid = set(values) - allowed
    if invalid:
        raise HTTPException(400, f"invalid doc_kind values: {sorted(invalid)}")
    return values


def _utc_date_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time()).replace(tzinfo=timezone.utc)


def _utc_date_end(d: date) -> datetime:
    return datetime.combine(d, datetime.max.time()).replace(tzinfo=timezone.utc)


KNOWLEDGE_TIME_SQL = "COALESCE(n.published_at, n.ingested_at, n.created_at)"
ARTICLE_TIME_SQL = "COALESCE(art.published_at, art.ingested_at, art.created_at)"


def _published_at(node: dict[str, Any]) -> Optional[datetime]:
    return (
        node.get("published_at")
        or node.get("effective_at")
        or node.get("source_published_at")
        or node.get("captured_at")
        or node.get("ingested_at")
        or node.get("created_at")
    )


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _snippet(text: Optional[str], limit: int = 200) -> Optional[str]:
    if not text:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


_CITATION_TERM_RE = re.compile(r"[\w\u4e00-\u9fff]{2,}", re.UNICODE)


def _citation_prompt_body(full_body: str, claim: str, context: str | None, limit: int) -> str:
    if len(full_body) <= limit:
        return full_body

    raw_terms = _CITATION_TERM_RE.findall(f"{claim} {context or ''}".lower())
    terms = [t for t in dict.fromkeys(raw_terms) if len(t) >= 3 or any("\u4e00" <= c <= "\u9fff" for c in t)]
    if not terms:
        return full_body[:limit] + "..."

    lower_body = full_body.lower()
    hits: list[int] = []
    for term in sorted(terms, key=len, reverse=True)[:20]:
        start = 0
        while len(hits) < 30:
            idx = lower_body.find(term, start)
            if idx < 0:
                break
            hits.append(idx)
            start = idx + len(term)
    if not hits:
        return full_body[:limit] + "..."

    chunk_size = max(400, limit // min(len(hits), 3))
    ranges: list[tuple[int, int]] = []
    for hit in sorted(set(hits)):
        start = max(0, hit - chunk_size // 2)
        end = min(len(full_body), start + chunk_size)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
        if sum(end - start for start, end in ranges) >= limit:
            break

    chunks: list[str] = []
    remaining = limit
    for start, end in ranges:
        if remaining <= 0:
            break
        chunk = full_body[start:end][:remaining]
        marker = f"\n[excerpt {start}:{start + len(chunk)}]\n"
        chunks.append(marker + chunk)
        remaining -= len(chunk)
    return "\n".join(chunks).strip()


# ─────────────────────────────────────────────────────────────────────────
#                                  search
# ─────────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search(
    query: str = Query(..., min_length=1),
    top_k: Optional[int] = None,
    include_snippet: bool = True,
    type: Optional[str] = Query(None, description="csv: article,entity,summary,index"),
    tags: Optional[str] = Query(None, description="csv tag list"),
    source_ids: Optional[str] = Query(None, description="csv source ids"),
    doc_kind: Optional[str] = Query(None, description="csv doc_kind values"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    _: dict = Depends(require_auth_or_service_token),
):
    """Hybrid 向量 + 关键词搜索。filters 全部可选。"""
    top_k = top_k or config_loader.get("kb_public.search_top_k", 10)
    top_k_max = config_loader.get("kb_public.search_top_k_max", 50)
    top_k = min(max(int(top_k or 0), 1), top_k_max)

    type_list = _csv(type)
    tag_list = _csv(tags)
    source_id_list = _csv(source_ids)
    doc_kind_list = _validate_doc_kinds(_csv(doc_kind))

    embedding = await _embed_query(query)
    embedding_literal = _vector_literal(embedding)
    keyword_pattern = f"%{query.strip()}%"

    params: list[Any] = [USER_ID, keyword_pattern]
    conditions = [
        "n.user_id = $1",
        """
        (n.embedding IS NOT NULL
         OR s.body_embedding IS NOT NULL
         OR n.title ILIKE $2
         OR en.canonical_name ILIKE $2
         OR n.abstract ILIKE $2
         OR s.body ILIKE $2)
        """,
    ]
    if type_list:
        params.append(type_list)
        conditions.append(f"n.object_type = ANY(${len(params)}::text[])")
    if tag_list:
        params.append(tag_list)
        conditions.append(f"n.tags && ${len(params)}::text[]")
    if source_id_list:
        params.append(source_id_list)
        conditions.append(f"n.source_id = ANY(${len(params)}::text[])")
    if doc_kind_list:
        params.append(doc_kind_list)
        conditions.append(f"n.doc_kind = ANY(${len(params)}::text[])")
    if date_from:
        params.append(_utc_date_start(date_from))
        conditions.append(f"{KNOWLEDGE_TIME_SQL} >= ${len(params)}")
    if date_to:
        params.append(_utc_date_end(date_to))
        conditions.append(f"{KNOWLEDGE_TIME_SQL} <= ${len(params)}")

    params.append(top_k)
    limit_idx = len(params)

    # 双重使用同一向量子查询会重复昂贵 cosine 计算。这里采用 CTE 让 PG 自动复用。
    sql = f"""
        WITH scored AS (
            SELECT n.id, COALESCE(en.canonical_name, n.title) AS title,
                   n.object_type AS type, n.doc_kind, n.tags, n.source_id,
                   COALESCE(s.body, n.abstract) AS abstract,
                   {KNOWLEDGE_TIME_SQL} AS published_at,
                   COALESCE(
                     CASE
                       WHEN n.object_type = 'summary' THEN
                         0.75 * (1 - (COALESCE(s.body_embedding, n.embedding) <=> '{embedding_literal}'::vector))
                         + 0.25 * (1 - (COALESCE(s.perspective_embedding, s.body_embedding, n.embedding) <=> '{embedding_literal}'::vector))
                       ELSE
                         1 - (n.embedding <=> '{embedding_literal}'::vector)
                     END,
                     0
                   ) AS vector_score,
                   (n.title ILIKE $2 OR en.canonical_name ILIKE $2 OR n.abstract ILIKE $2 OR s.body ILIKE $2) AS keyword_hit
            FROM knowledge_nodes n
            LEFT JOIN summary_nodes s ON s.node_id = n.id
            LEFT JOIN entity_nodes en ON en.node_id = n.id
            WHERE {' AND '.join(conditions)}
        )
        SELECT *,
               vector_score + CASE WHEN keyword_hit THEN 0.15 ELSE 0 END AS final_score
        FROM scored
        ORDER BY final_score DESC
        LIMIT ${limit_idx}
    """

    async with database.database.connection() as conn:
        rows = await conn.raw_connection.fetch(sql, *params)

    results = []
    for r in rows:
        vector_score = float(r["vector_score"] or 0)
        keyword_hit = bool(r["keyword_hit"])
        if keyword_hit and vector_score > 0.3:
            why = "hybrid"
        elif keyword_hit:
            why = "keyword"
        else:
            why = "vector"
        results.append({
            "id": r["id"],
            "type": r["type"],
            "title": r["title"],
            "doc_kind": r["doc_kind"],
            "snippet": _snippet(r["abstract"]) if include_snippet else None,
            "score": float(r["final_score"] or 0),
            "why_matched": why,
            "tags": list(r["tags"]) if r["tags"] else [],
            "published_at": _iso(r["published_at"]),
        })

    return {"results": results}


# ─────────────────────────────────────────────────────────────────────────
#                                  fetch
# ─────────────────────────────────────────────────────────────────────────

class FetchBatchRequest(BaseModel):
    ids: list[str]
    include_body: bool = True
    include_related_ids: bool = False


async def _fetch_one(
    node_id: str,
    include_body: bool,
    include_related_ids: bool,
) -> Optional[dict[str, Any]]:
    node = await object_nodes.fetch_node_with_object_fields(node_id)
    if not node or node.get("user_id") != USER_ID:
        return None
    for key in ("embedding", "body_embedding", "perspective_embedding"):
        node.pop(key, None)

    object_type = node.get("object_type") or "article"

    source_name = None
    if node.get("source_id"):
        src_row = await database.database.fetch_one(
            "SELECT name FROM sources WHERE id = :id", {"id": node["source_id"]}
        )
        if src_row:
            source_name = src_row["name"]

    summary_rows = await database.database.fetch_all(
        """
        SELECT s.node_id AS id, s.perspective_label,
               LEFT(s.body, 200) AS body_snippet, s.is_default
        FROM summary_nodes s
        WHERE s.summary_of = :id
        ORDER BY s.is_default DESC, s.created_at ASC
        """,
        {"id": node_id},
    )
    summaries = [
        {
            "id": r["id"],
            "perspective_label": r["perspective_label"],
            "body_snippet": r["body_snippet"],
            "is_default": bool(r["is_default"]),
        }
        for r in summary_rows
    ]

    outline: list[dict[str, Any]] = []
    if object_type == "index":
        child_rows = await database.database.fetch_all(
            """
            SELECT ic.child_id AS id, ic.position, n.title, n.object_type AS type
            FROM index_children ic
            JOIN knowledge_nodes n ON n.id = ic.child_id
            WHERE ic.index_id = :id
            ORDER BY ic.position ASC
            """,
            {"id": node_id},
        )
        outline = [
            {"id": r["id"], "title": r["title"], "position": r["position"], "type": r["type"]}
            for r in child_rows
        ]

    result: dict[str, Any] = {
        "id": node_id,
        "type": object_type,
        "title": node.get("title"),
        "doc_kind": node.get("doc_kind"),
        "abstract": node.get("abstract"),
        "tags": list(node.get("tags") or []),
        "published_at": _iso(_published_at(node)),
        "source_id": node.get("source_id"),
        "source_name": source_name,
        "summaries": summaries,
        "outline": outline,
    }

    if include_body:
        body_chars = config_loader.get("kb_public.fetch_body_chars", 100000)
        result["body"] = (
            _read_wiki_body(USER_ID, node_id, object_type, limit=body_chars)
            if object_type == "article" else None
        )

    if include_related_ids:
        edge_rows = await database.database.fetch_all(
            """
            SELECT from_node_id, to_node_id, relation_type
            FROM knowledge_edges
            WHERE from_node_id = :id OR to_node_id = :id
            """,
            {"id": node_id},
        )
        related = []
        for e in edge_rows:
            if not _is_visible_edge(e["relation_type"]):
                continue
            other = e["to_node_id"] if e["from_node_id"] == node_id else e["from_node_id"]
            related.append({"id": other, "relation": e["relation_type"]})
        result["related_ids"] = related

    return result


@router.get("/nodes/{node_id}")
async def fetch_node(
    node_id: str,
    include_body: bool = True,
    include_related_ids: bool = False,
    _: dict = Depends(require_auth_or_service_token),
):
    result = await _fetch_one(node_id, include_body, include_related_ids)
    if not result:
        raise HTTPException(404, f"node {node_id} not found")
    return result


@router.post("/nodes/batch")
async def fetch_nodes_batch(
    body: FetchBatchRequest,
    _: dict = Depends(require_auth_or_service_token),
):
    max_batch = config_loader.get("kb_public.fetch_max_batch", 20)
    if len(body.ids) > max_batch:
        raise HTTPException(400, f"max {max_batch} ids per batch")
    nodes = []
    for nid in body.ids:
        item = await _fetch_one(nid, body.include_body, body.include_related_ids)
        if item:
            nodes.append(item)
    return {"nodes": nodes}


# ─────────────────────────────────────────────────────────────────────────
#                                 related
# ─────────────────────────────────────────────────────────────────────────

_RELATED_SQL: dict[str, str] = {
    "mentions": """
        SELECT n.id, n.title, n.object_type AS type, n.doc_kind, ke.weight,
               COALESCE(n.published_at, n.ingested_at, n.created_at) AS published_at
        FROM knowledge_edges ke
        JOIN knowledge_nodes n ON n.id = ke.to_node_id
        WHERE ke.from_node_id = :id AND ke.relation_type = 'mentions'
        ORDER BY ke.weight DESC NULLS LAST, n.created_at DESC
        LIMIT :limit
    """,
    "mentioned_by": """
        SELECT n.id, n.title, n.object_type AS type, n.doc_kind, ke.weight,
               COALESCE(n.published_at, n.ingested_at, n.created_at) AS published_at
        FROM knowledge_edges ke
        JOIN knowledge_nodes n ON n.id = ke.from_node_id
        WHERE ke.to_node_id = :id AND ke.relation_type = 'mentions'
        ORDER BY ke.weight DESC NULLS LAST, n.created_at DESC
        LIMIT :limit
    """,
    "summarizes": """
        SELECT n.id, n.title, n.object_type AS type, n.doc_kind, 1.0::float AS weight,
               COALESCE(n.published_at, n.ingested_at, n.created_at) AS published_at
        FROM summary_nodes s
        JOIN knowledge_nodes n ON n.id = s.summary_of
        WHERE s.node_id = :id
        LIMIT :limit
    """,
    "summarized_by": """
        SELECT n.id, n.title, n.object_type AS type, n.doc_kind, 1.0::float AS weight,
               n.created_at AS published_at
        FROM summary_nodes s
        JOIN knowledge_nodes n ON n.id = s.node_id
        WHERE s.summary_of = :id
        ORDER BY s.is_default DESC, n.created_at ASC
        LIMIT :limit
    """,
    "contains": """
        SELECT n.id, n.title, n.object_type AS type, n.doc_kind, ic.position::float AS weight,
               COALESCE(n.published_at, n.ingested_at, n.created_at) AS published_at
        FROM index_children ic
        JOIN knowledge_nodes n ON n.id = ic.child_id
        WHERE ic.index_id = :id
        ORDER BY ic.position ASC
        LIMIT :limit
    """,
    "part_of": """
        SELECT n.id, n.title, n.object_type AS type, n.doc_kind, 1.0::float AS weight,
               n.created_at AS published_at
        FROM index_children ic
        JOIN knowledge_nodes n ON n.id = ic.index_id
        WHERE ic.child_id = :id
        LIMIT :limit
    """,
}


@router.get("/nodes/{node_id}/related")
async def related(
    node_id: str,
    relation: Relation,
    limit: Optional[int] = None,
    _: dict = Depends(require_auth_or_service_token),
):
    limit = limit or config_loader.get("kb_public.related_default_limit", 20)
    limit_max = config_loader.get("kb_public.related_limit_max", 100)
    limit = min(max(int(limit or 0), 1), limit_max)

    rows = await database.database.fetch_all(
        _RELATED_SQL[relation], {"id": node_id, "limit": limit}
    )
    return {
        "results": [
            {
                "id": r["id"],
                "type": r["type"],
                "title": r["title"],
                "doc_kind": r["doc_kind"],
                "relation": relation,
                "weight": float(r["weight"]) if r["weight"] is not None else 0.0,
                "published_at": _iso(r["published_at"]),
            }
            for r in rows
        ]
    }


# ─────────────────────────────────────────────────────────────────────────
#                                 timeline
# ─────────────────────────────────────────────────────────────────────────

@router.get("/timeline")
async def timeline(
    entity_id: Optional[str] = None,
    topic_query: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: Optional[int] = None,
    include_facts: bool = False,
    _: dict = Depends(require_auth_or_service_token),
):
    if not entity_id and not topic_query:
        raise HTTPException(400, "must provide entity_id or topic_query")

    limit = limit or config_loader.get("kb_public.timeline_default_limit", 50)
    limit_max = config_loader.get("kb_public.timeline_limit_max", 200)
    limit = min(max(int(limit or 0), 1), limit_max)

    if entity_id:
        clauses = ["ke.relation_type = 'mentions'", "ke.to_node_id = :entity_id"]
        params: dict[str, Any] = {"entity_id": entity_id, "limit": limit}
        if date_from:
            params["date_from"] = _utc_date_start(date_from)
            clauses.append(f"{ARTICLE_TIME_SQL} >= :date_from")
        if date_to:
            params["date_to"] = _utc_date_end(date_to)
            clauses.append(f"{ARTICLE_TIME_SQL} <= :date_to")

        sql = f"""
            SELECT art.id AS article_id, art.title, art.doc_kind,
                   {ARTICLE_TIME_SQL} AS published_at,
                   src.name AS source_name
            FROM knowledge_edges ke
            JOIN knowledge_nodes art ON art.id = ke.from_node_id AND art.object_type = 'article'
            LEFT JOIN sources src ON src.id = art.source_id
            WHERE {' AND '.join(clauses)}
            ORDER BY published_at DESC NULLS LAST
            LIMIT :limit
        """
        rows = await database.database.fetch_all(sql, params)

        events = [
            {
                "published_at": _iso(r["published_at"]),
                "article_id": r["article_id"],
                "title": r["title"],
                "doc_kind": r["doc_kind"],
                "source_name": r["source_name"],
            }
            for r in rows
        ]

        if include_facts:
            fact_rows = await database.database.fetch_all(
                """
                SELECT ef.article_id, ef.fact_text, ef.evidence_span, ef.fact_time, ef.confidence
                FROM entity_facts ef
                WHERE ef.entity_id = :entity_id
                ORDER BY ef.fact_time DESC NULLS LAST
                """,
                {"entity_id": entity_id},
            )
            facts_by_article: dict[str, list[dict[str, Any]]] = {}
            for f in fact_rows:
                facts_by_article.setdefault(f["article_id"], []).append({
                    "fact_text": f["fact_text"],
                    "evidence_span": f["evidence_span"],
                    "fact_time": _iso(f["fact_time"]),
                    "confidence": float(f["confidence"] or 0),
                })
            for ev in events:
                ev["facts"] = facts_by_article.get(ev["article_id"], [])

        return {"events": events}

    # topic_query 路径：向量搜索文章（non-None guaranteed by earlier validation）
    assert topic_query
    embedding = await _embed_query(topic_query)
    embedding_literal = _vector_literal(embedding)
    threshold = config_loader.get("kb_public.timeline_min_score", 0.3)

    params_list: list[Any] = [USER_ID]
    conds = [
        "n.user_id = $1",
        "n.object_type = 'article'",
        "n.embedding IS NOT NULL",
        f"(1 - (n.embedding <=> '{embedding_literal}'::vector)) >= {threshold}",
    ]
    if date_from:
        params_list.append(_utc_date_start(date_from))
        conds.append(f"{KNOWLEDGE_TIME_SQL} >= ${len(params_list)}")
    if date_to:
        params_list.append(_utc_date_end(date_to))
        conds.append(f"{KNOWLEDGE_TIME_SQL} <= ${len(params_list)}")

    params_list.append(limit)
    limit_idx = len(params_list)

    sql = f"""
        SELECT n.id AS article_id, n.title, n.doc_kind,
               {KNOWLEDGE_TIME_SQL} AS published_at,
               src.name AS source_name
        FROM knowledge_nodes n
        LEFT JOIN sources src ON src.id = n.source_id
        WHERE {' AND '.join(conds)}
        ORDER BY published_at DESC NULLS LAST
        LIMIT ${limit_idx}
    """

    async with database.database.connection() as conn:
        rows = await conn.raw_connection.fetch(sql, *params_list)

    return {
        "events": [
            {
                "published_at": _iso(r["published_at"]),
                "article_id": r["article_id"],
                "title": r["title"],
                "doc_kind": r["doc_kind"],
                "source_name": r["source_name"],
            }
            for r in rows
        ]
    }


# ─────────────────────────────────────────────────────────────────────────
#                       compare / cite / summarize_corpus
#                       共用：读取节点上下文用于 LLM 输入
# ─────────────────────────────────────────────────────────────────────────

async def _load_doc_context(node_id: str, body_chars: int) -> Optional[dict[str, Any]]:
    node = await object_nodes.fetch_node_with_object_fields(node_id)
    if not node or node.get("user_id") != USER_ID:
        return None
    object_type = node.get("object_type") or "article"
    body_text = ""
    if object_type == "article":
        summary_row = await database.database.fetch_one(
            """
            SELECT body
            FROM summary_nodes
            WHERE summary_of = :id
              AND body IS NOT NULL
              AND length(body) > 0
            ORDER BY is_default DESC, created_at ASC
            LIMIT 1
            """,
            {"id": node_id},
        )
        if summary_row and summary_row["body"]:
            body_text = _snippet(summary_row["body"], body_chars) or ""
        else:
            body_text = _read_wiki_body(USER_ID, node_id, "article", limit=body_chars)
    elif object_type == "summary":
        body_text = (node.get("abstract") or "")[:body_chars]
    return {
        "id": node_id,
        "title": node.get("title") or "",
        "doc_kind": node.get("doc_kind"),
        "published_at": _published_at(node),
        "abstract": node.get("abstract") or "",
        "body": body_text,
        "type": object_type,
    }


def _docs_to_prompt_text(docs: list[dict[str, Any]]) -> str:
    chunks = []
    for d in docs:
        chunk = f"\n[{d['id']}] {d['title']}（doc_kind: {d['doc_kind'] or '未标注'}）\n"
        if d.get("abstract"):
            chunk += f"摘要：{d['abstract']}\n"
        if d.get("body"):
            chunk += f"正文节选：\n{d['body']}\n"
        chunk += "---"
        chunks.append(chunk)
    return "\n".join(chunks)


def _source_descriptor(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": d["id"],
        "title": d["title"],
        "doc_kind": d.get("doc_kind"),
        "published_at": _iso(d.get("published_at")),
    }


# ─────────────────────────────────────────────────────────────────────────
#                                 compare
# ─────────────────────────────────────────────────────────────────────────

class CompareRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=2, max_length=5)
    dimensions: Optional[list[str]] = None
    focus: Optional[str] = None


@router.post("/compare")
async def compare(
    body: CompareRequest,
    _: dict = Depends(require_auth_or_service_token),
):
    body_chars = config_loader.get("kb_public.compare_body_chars", 4000)
    docs: list[dict[str, Any]] = []
    for nid in body.node_ids:
        d = await _load_doc_context(nid, body_chars)
        if not d:
            raise HTTPException(404, f"node {nid} not found")
        docs.append(d)

    dims_text = (
        "\n".join(f"- {d}" for d in body.dimensions)
        if body.dimensions else "（自动判断关键维度）"
    )
    prompt = prompt_loader.fill(
        "compare_nodes",
        documents=_docs_to_prompt_text(docs),
        dimensions=dims_text,
        focus=body.focus or "（无）",
    )

    resp = await claude_client.messages.create(
        model=config_loader.get("models.compare", "claude-sonnet-4-6"),
        max_tokens=config_loader.get("llm_output_tokens.compare", 2048),
        messages=[{"role": "user", "content": prompt}],
    )
    output = getattr(resp.content[0], "text", "").strip()

    # 简单切分表格与分析：找到表格末尾（最后一个 | 开头的行），之后归为 analysis
    lines = output.split("\n")
    last_table_line = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|"):
            last_table_line = i
    if last_table_line >= 0:
        comparison_table = "\n".join(lines[: last_table_line + 1]).strip()
        analysis = "\n".join(lines[last_table_line + 1 :]).strip()
    else:
        comparison_table = ""
        analysis = output

    return {
        "comparison_table": comparison_table,
        "analysis": analysis,
        "sources_used": [_source_descriptor(d) for d in docs],
    }


# ─────────────────────────────────────────────────────────────────────────
#                                  cite
# ─────────────────────────────────────────────────────────────────────────

class CiteRequest(BaseModel):
    claim: str = Field(..., min_length=1)
    context: Optional[str] = None
    doc_kinds: Optional[list[DocKind]] = None
    max_results: Optional[int] = None


def _extract_json_array(text: str) -> list[Any]:
    """Strip code fences / preamble and parse the first JSON array."""
    text = text.strip()
    if text.startswith("```"):
        # remove leading fence
        text = text.split("\n", 1)[1] if "\n" in text else text
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


@router.post("/cite")
async def cite(
    body: CiteRequest,
    _: dict = Depends(require_auth_or_service_token),
):
    max_results = body.max_results or config_loader.get("kb_public.cite_max_results", 5)
    candidate_count = config_loader.get("kb_public.cite_candidate_count", 20)
    body_chars = config_loader.get("kb_public.cite_body_chars", 3000)

    # Stage 1: 向量粗筛
    embedding = await _embed_query(body.claim)
    embedding_literal = _vector_literal(embedding)
    doc_kind_list = _validate_doc_kinds(list(body.doc_kinds) if body.doc_kinds else [])

    params: list[Any] = [USER_ID]
    conds = [
        "n.user_id = $1",
        "n.object_type = 'article'",
        "n.embedding IS NOT NULL",
    ]
    if doc_kind_list:
        params.append(doc_kind_list)
        conds.append(f"n.doc_kind = ANY(${len(params)}::text[])")

    params.append(candidate_count)
    limit_idx = len(params)

    sql = f"""
        SELECT n.id, n.title, n.doc_kind, n.abstract,
               {KNOWLEDGE_TIME_SQL} AS published_at,
               1 - (n.embedding <=> '{embedding_literal}'::vector) AS score
        FROM knowledge_nodes n
        WHERE {' AND '.join(conds)}
        ORDER BY n.embedding <=> '{embedding_literal}'::vector
        LIMIT ${limit_idx}
    """
    async with database.database.connection() as conn:
        rows = await conn.raw_connection.fetch(sql, *params)

    if not rows:
        return {"citations": []}

    # LLM 输入按预算从全文抽取相关窗口；quote 验证使用全文。
    body_texts: dict[str, str] = {}
    row_index: dict[str, Any] = {}
    docs_for_prompt: list[dict[str, Any]] = []
    for r in rows:
        nid = r["id"]
        full_body = _read_wiki_body(USER_ID, nid, "article", limit=None)
        prompt_body = _citation_prompt_body(full_body, body.claim, body.context, body_chars)
        body_texts[nid] = full_body
        row_index[nid] = r
        docs_for_prompt.append({
            "id": nid,
            "title": r["title"],
            "doc_kind": r["doc_kind"],
            "abstract": r["abstract"] or "",
            "body": prompt_body,
        })

    # Stage 2: LLM 精确匹配
    prompt = prompt_loader.fill(
        "cite_match",
        claim=body.claim,
        context=body.context or "（无）",
        candidates=_docs_to_prompt_text(docs_for_prompt),
    )
    resp = await claude_client.messages.create(
        model=config_loader.get("models.cite", "claude-sonnet-4-6"),
        max_tokens=config_loader.get("llm_output_tokens.cite", 2048),
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = getattr(resp.content[0], "text", "")
    candidate_quotes = _extract_json_array(raw_text)

    # 服务端验证：quote 必须逐字出现在正文中（防 LLM 幻觉）
    verified: list[dict[str, Any]] = []
    for q in candidate_quotes:
        if not isinstance(q, dict):
            continue
        aid = q.get("article_id")
        quote = q.get("quote") or ""
        if not aid or not quote or aid not in body_texts:
            continue
        if quote not in body_texts[aid]:
            continue
        r = row_index.get(aid)
        if not r:
            continue
        verified.append({
            "article_id": aid,
            "title": r["title"],
            "doc_kind": r["doc_kind"],
            "published_at": _iso(r["published_at"]),
            "quote": quote,
            "relevance_explanation": q.get("relevance_explanation") or "",
            "confidence": float(q.get("confidence") or 0),
        })
        if len(verified) >= max_results:
            break

    return {"citations": verified}


# ─────────────────────────────────────────────────────────────────────────
#                            summarize_corpus
# ─────────────────────────────────────────────────────────────────────────

class SummarizeCorpusRequest(BaseModel):
    node_ids: Optional[list[str]] = None
    query: Optional[str] = None
    max_sources: Optional[int] = None
    focus: Optional[str] = None
    output_format: OutputFormat = "prose"


@router.post("/summarize_corpus")
async def summarize_corpus(
    body: SummarizeCorpusRequest,
    _: dict = Depends(require_auth_or_service_token),
):
    if not body.node_ids and not body.query:
        raise HTTPException(400, "must provide node_ids or query")

    max_sources = body.max_sources or config_loader.get("kb_public.summarize_max_sources", 10)
    body_chars = config_loader.get("kb_public.summarize_body_chars", 2500)

    # 解析语料 ids
    if body.node_ids:
        node_ids = body.node_ids[:max_sources]
    else:
        # Summary-first 分层检索：
        #   Stage 1：在 summary_nodes 向量搜索 → top-K summaries
        #   Stage 2：展开到对应的 summary_of（即原文章）
        #   Fallback：若 summary 命中不足阈值/数量，补充直接 article 向量搜索
        assert body.query  # non-None guaranteed by earlier validation
        embedding = await _embed_query(body.query)
        embedding_literal = _vector_literal(embedding)
        min_score = config_loader.get("kb_public.summarize_summary_min_score", 0.3)

        summary_rows = await database.database.fetch_all(
            f"""
            SELECT s.summary_of AS article_id,
                   1 - (s.body_embedding <=> '{embedding_literal}'::vector) AS score
            FROM summary_nodes s
            JOIN knowledge_nodes n ON n.id = s.node_id
            WHERE n.user_id = :user_id
              AND s.body_embedding IS NOT NULL
              AND s.summary_of IS NOT NULL
              AND (1 - (s.body_embedding <=> '{embedding_literal}'::vector)) >= :min_score
            ORDER BY s.body_embedding <=> '{embedding_literal}'::vector
            LIMIT :limit
            """,
            {"user_id": USER_ID, "limit": max_sources, "min_score": min_score},
        )
        node_ids = []
        seen: set[str] = set()
        for r in summary_rows:
            aid = r["article_id"]
            if aid and aid not in seen:
                seen.add(aid)
                node_ids.append(aid)

        # Fallback：summary 路径未填满时，补足直接 article 搜索
        if len(node_ids) < max_sources:
            remaining = max_sources - len(node_ids)
            article_rows = await database.database.fetch_all(
                f"""
                SELECT n.id
                FROM knowledge_nodes n
                WHERE n.user_id = :user_id
                  AND n.object_type = 'article'
                  AND n.embedding IS NOT NULL
                  AND NOT (n.id = ANY(:exclude_ids))
                ORDER BY n.embedding <=> '{embedding_literal}'::vector
                LIMIT :limit
                """,
                {"user_id": USER_ID, "exclude_ids": list(seen), "limit": remaining},
            )
            for r in article_rows:
                node_ids.append(r["id"])

    if not node_ids:
        return {"summary": "", "sources_used": [], "coverage_note": "未找到匹配的文档"}

    docs: list[dict[str, Any]] = []
    for nid in node_ids:
        d = await _load_doc_context(nid, body_chars)
        if d:
            docs.append(d)

    if not docs:
        return {"summary": "", "sources_used": [], "coverage_note": "未找到匹配的文档"}

    prompt = prompt_loader.fill(
        "summarize_corpus",
        documents=_docs_to_prompt_text(docs),
        focus=body.focus or "（整体综述）",
        output_format=body.output_format,
    )
    resp = await claude_client.messages.create(
        model=config_loader.get("models.summarize_corpus", "claude-sonnet-4-6"),
        max_tokens=config_loader.get("llm_output_tokens.summarize_corpus", 3000),
        messages=[{"role": "user", "content": prompt}],
    )
    summary_text = getattr(resp.content[0], "text", "").strip()

    pub_years = [d["published_at"].year for d in docs if isinstance(d.get("published_at"), datetime)]
    if pub_years:
        coverage = f"基于 {len(docs)} 篇文档，时间跨度 {min(pub_years)}-{max(pub_years)}"
    else:
        coverage = f"基于 {len(docs)} 篇文档"

    return {
        "summary": summary_text,
        "sources_used": [_source_descriptor(d) for d in docs],
        "coverage_note": coverage,
    }

import json
import os
import pathlib
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
from openai import AsyncOpenAI

import database
from kb.graph import fetch_node_with_object_fields
from settings import settings
from prompts import prompts

USER_ID = "default"
USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
LEGACY_LLM_EDGE_TYPES = {"extends", "background_of", "supports", "contradicts", "part_of"}

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
claude_client = anthropic.AsyncAnthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))


READ_ONLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "kb_search",
        "description": "Search the user's knowledge base. Read-only. Use filters for object_type, source_type, or time ranges when available.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "object_type": {"type": "string", "enum": ["article", "summary", "entity", "index"]},
                "source_type": {"type": "string"},
                "since": {"type": "string", "description": "ISO date/datetime lower bound"},
                "until": {"type": "string", "description": "ISO date/datetime upper bound"},
                "lookback_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "Relative time window ending now; use 24 for the past 24 hours."},
                "time_basis": {"type": "string", "enum": ["knowledge", "published", "captured"], "description": "Use published to filter by source_published_at; use captured for captured_at, such as articles captured/ingested into the system in the past N hours; otherwise uses effective/source/captured/ingested knowledge time."},
                "sort": {"type": "string", "enum": ["relevance", "time_desc"], "description": "Use time_desc when the user asks for latest/recent items to list."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kb_get_node",
        "description": "Open one knowledge node by id and return details plus wiki body excerpt. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "kb_get_neighbors",
        "description": "Return visible graph neighbors around a node. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 2},
            },
            "required": ["id"],
        },
    },
    {
        "name": "kb_get_sources",
        "description": "Return source metadata for a knowledge node. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in values) + "]"


def _is_visible_edge(relation_type: str | None) -> bool:
    return relation_type not in LEGACY_LLM_EDGE_TYPES


async def _embed_text(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model=settings.embedding.model,
        input=text[: settings.embedding.max_chars],
        dimensions=settings.embedding.dimensions,
    )
    return resp.data[0].embedding


async def _embed_query(text: str) -> list[float]:
    if not settings.retrieval.use_hyde:
        return await _embed_text(text)
    try:
        hypo = await claude_client.messages.create(
            model=settings.models.hyde_abstract,
            max_tokens=settings.llm_output_tokens.hyde_abstract,
            messages=[{"role": "user", "content": prompts.hyde_abstract(topic=text)}],
        )
        hypo_text = getattr(hypo.content[0], "text", "").strip()
        if hypo_text:
            return await _embed_text(hypo_text)
    except Exception:
        pass
    return await _embed_text(text)


def _wiki_file_path(user_id: str, node_id: str, object_type: str) -> pathlib.Path:
    subdir = {
        "article": "articles",
        "entity": "entities",
        "summary": "summaries",
        "index": "indices",
    }.get(object_type, "articles")
    return USER_DATA_DIR / user_id / "wiki" / subdir / f"{node_id}.md"


def _read_wiki_body(user_id: str, node_id: str, object_type: str, limit: int = 4000) -> str:
    path = _wiki_file_path(user_id, node_id, object_type)
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    body = parts[2].strip() if len(parts) >= 3 else raw.strip()
    for marker in ("\n## 关联节点\n", "\n## 関連節点\n"):
        if marker in body:
            body = body[: body.index(marker)].strip()
    lines = body.split("\n", 2)
    if len(lines) >= 3:
        body = lines[2].strip()
    return body[:limit] + ("..." if len(body) > limit else "")


def _reference(node: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    ref = {
        "id": node.get("id"),
        "title": node.get("title") or node.get("id"),
        "object_type": node.get("object_type"),
        "source_type": node.get("source_type"),
    }
    if score is not None:
        ref["score"] = score
    return ref


def _time_expr(time_basis: str | None) -> str:
    if time_basis == "published":
        return "an.source_published_at"
    if time_basis == "captured":
        return "an.captured_at"
    return "COALESCE(n.published_at, n.ingested_at, n.created_at)"


def _parse_time_filter(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_time_filters(filters: dict[str, Any], now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = _parse_time_filter(filters.get("since"))
    until = _parse_time_filter(filters.get("until"))

    if since is None:
        try:
            lookback_hours = int(filters.get("lookback_hours") or 0)
        except (TypeError, ValueError):
            lookback_hours = 0
        if lookback_hours > 0:
            since = now - timedelta(hours=min(lookback_hours, 168))
            if until is None:
                until = now

    return since, until


def _time_filter_clause(
    params: list[Any],
    since: datetime | str | None,
    until: datetime | str | None,
    time_basis: str | None = None,
) -> str:
    clauses: list[str] = []
    time_expr = _time_expr(time_basis)
    since = _parse_time_filter(since)
    until = _parse_time_filter(until)
    if since:
        params.append(since)
        clauses.append(f"{time_expr} >= ${len(params)}::timestamptz")
    if until:
        params.append(until)
        clauses.append(f"{time_expr} <= ${len(params)}::timestamptz")
    return " AND ".join(clauses)


async def search(query: str, filters: dict[str, Any] | None = None, user_id: str = USER_ID) -> dict[str, Any]:
    filters = filters or {}
    now = datetime.now(timezone.utc)
    limit = min(max(int(filters.get("limit") or 5), 1), 10)
    object_type = filters.get("object_type")
    source_type = filters.get("source_type")
    time_basis = filters.get("time_basis")
    sort = filters.get("sort") or "relevance"
    since, until = _resolve_time_filters(filters, now=now)
    time_expr = _time_expr(time_basis)

    params: list[Any] = [user_id]
    conditions = [
        "n.user_id = $1",
        "(n.embedding IS NOT NULL OR s.body_embedding IS NOT NULL)",
    ]
    if object_type:
        params.append(object_type)
        conditions.append(f"n.object_type = ${len(params)}")
    if source_type:
        params.append(source_type)
        conditions.append(f"COALESCE(an.source_type, n.object_type) = ${len(params)}")
    time_clause = _time_filter_clause(params, since, until, time_basis)
    if time_clause:
        conditions.append(time_clause)
    if time_basis == "published":
        conditions.append("an.source_published_at IS NOT NULL")

    try:
        embedding = await _embed_query(query)
        embedding_literal = _vector_literal(embedding)
        limit_idx = len(params) + 1
        order_by = "effective_time DESC NULLS LAST, score DESC" if sort == "time_desc" else "score DESC"
        async with database.database.connection() as conn:
            rows = await conn.raw_connection.fetch(
                f"""
                SELECT n.id, COALESCE(en.canonical_name, n.title) AS title, COALESCE(s.body, n.abstract) AS abstract,
                       COALESCE(an.source_type, n.object_type) AS source_type,
                       n.tags, n.object_type,
                       {time_expr} AS effective_time,
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
                LEFT JOIN entity_nodes en ON en.node_id = n.id
                WHERE {" AND ".join(conditions)}
                ORDER BY {order_by}
                LIMIT ${limit_idx}
                """,
                *params,
                limit,
            )
    except Exception:
        text_params = [user_id, f"%{query.strip()}%"]
        text_conditions = ["n.user_id = $1", "(n.title ILIKE $2 OR en.canonical_name ILIKE $2 OR n.abstract ILIKE $2)"]
        if object_type:
            text_params.append(object_type)
            text_conditions.append(f"n.object_type = ${len(text_params)}")
        if source_type:
            text_params.append(source_type)
            text_conditions.append(f"COALESCE(an.source_type, n.object_type) = ${len(text_params)}")
        time_clause = _time_filter_clause(text_params, since, until, time_basis)
        if time_clause:
            text_conditions.append(time_clause)
        if time_basis == "published":
            text_conditions.append("an.source_published_at IS NOT NULL")
        limit_idx = len(text_params) + 1
        async with database.database.connection() as conn:
            rows = await conn.raw_connection.fetch(
                f"""
                SELECT n.id, COALESCE(en.canonical_name, n.title) AS title, n.abstract,
                       COALESCE(an.source_type, n.object_type) AS source_type,
                       n.tags, n.object_type,
                       {time_expr} AS effective_time,
                       NULL::float AS score
                FROM knowledge_nodes n
                LEFT JOIN article_nodes an ON an.node_id = n.id
                LEFT JOIN entity_nodes en ON en.node_id = n.id
                WHERE {" AND ".join(text_conditions)}
                ORDER BY effective_time DESC
                LIMIT ${limit_idx}
                """,
                *text_params,
                limit,
            )

    results = [
        {
            "id": r["id"],
            "title": r["title"],
            "abstract": r["abstract"],
            "source_type": r["source_type"],
            "tags": list(r["tags"]) if r["tags"] else [],
            "object_type": r["object_type"],
            "effective_time": r["effective_time"].isoformat() if r["effective_time"] else None,
            "score": float(r["score"]) if r["score"] is not None else None,
        }
        for r in rows
    ]
    return {
        "tool": "kb_search",
        "query": query,
        "resolved_time": {
            "now_utc": now.isoformat(),
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "time_basis": time_basis or "knowledge",
        },
        "results": results,
        "references": [_reference(r, r.get("score")) for r in results],
    }


async def get_node(node_id: str, user_id: str = USER_ID) -> dict[str, Any]:
    node = await fetch_node_with_object_fields(node_id)
    if not node or node.get("user_id") != user_id:
        return {"tool": "kb_get_node", "error": "node not found", "references": []}
    for key in ("embedding", "body_embedding", "perspective_embedding"):
        node.pop(key, None)
    if node.get("raw_ref") and isinstance(node["raw_ref"], str):
        node["raw_ref"] = json.loads(node["raw_ref"])
    object_type = node.get("object_type") or "article"
    node["wiki_body"] = _read_wiki_body(user_id, node_id, object_type)
    node = _jsonable(node)
    return {"tool": "kb_get_node", "node": node, "references": [_reference(node)]}


async def get_neighbors(node_id: str, depth: int = 1, user_id: str = USER_ID) -> dict[str, Any]:
    depth = min(max(int(depth or 1), 1), 2)
    visited_nodes: set[str] = set()
    visited_edges: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(node_id, 0)])
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    while queue:
        current_id, current_depth = queue.popleft()
        if current_id in visited_nodes:
            continue
        visited_nodes.add(current_id)
        row = await database.database.fetch_one(
            """
            SELECT n.id, n.title, n.abstract,
                   COALESCE(an.source_type, n.object_type) AS source_type,
                   n.tags, n.object_type, n.created_at
            FROM knowledge_nodes n
            LEFT JOIN article_nodes an ON an.node_id = n.id
            WHERE n.id = :id AND n.user_id = :user_id
            """,
            {"id": current_id, "user_id": user_id},
        )
        if not row:
            continue
        node = dict(row)
        if node.get("created_at"):
            node["created_at"] = node["created_at"].isoformat()
        nodes.append(node)
        if current_depth >= depth:
            continue

        edge_rows = await database.database.fetch_all(
            "SELECT id, from_node_id, to_node_id, relation_type, weight FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id",
            {"id": current_id},
        )
        structure_rows = await database.database.fetch_all(
            """
            SELECT index_id AS from_node_id, child_id AS to_node_id
            FROM index_children
            WHERE index_id = :id OR child_id = :id
            ORDER BY position ASC, created_at ASC
            """,
            {"id": current_id},
        )
        for edge in edge_rows:
            ed = dict(edge)
            if not _is_visible_edge(ed["relation_type"]):
                continue
            edge_key = f"edge:{ed['id']}"
            if edge_key not in visited_edges:
                visited_edges.add(edge_key)
                edges.append(ed)
            neighbor = ed["to_node_id"] if ed["from_node_id"] == current_id else ed["from_node_id"]
            if neighbor not in visited_nodes:
                queue.append((neighbor, current_depth + 1))
        for row in structure_rows:
            ed = {
                "id": f"contains:{row['from_node_id']}:{row['to_node_id']}",
                "from_node_id": row["from_node_id"],
                "to_node_id": row["to_node_id"],
                "relation_type": "contains",
                "weight": 1.0,
            }
            if ed["id"] not in visited_edges:
                visited_edges.add(ed["id"])
                edges.append(ed)
            neighbor = ed["to_node_id"] if ed["from_node_id"] == current_id else ed["from_node_id"]
            if neighbor not in visited_nodes:
                queue.append((neighbor, current_depth + 1))

    return {
        "tool": "kb_get_neighbors",
        "nodes": nodes,
        "edges": edges,
        "references": [_reference(n) for n in nodes],
    }


async def get_sources(node_id: str, user_id: str = USER_ID) -> dict[str, Any]:
    row = await database.database.fetch_one(
        """
        SELECT n.id, n.title, n.object_type, sn.summary_of, sn.source
        FROM knowledge_nodes n
        LEFT JOIN summary_nodes sn ON sn.node_id = n.id
        WHERE n.id = :id AND n.user_id = :user_id
        """,
        {"id": node_id, "user_id": user_id},
    )
    if not row:
        return {"tool": "kb_get_sources", "error": "node not found", "sources": [], "references": []}

    source_node_ids = [node_id]
    if row["summary_of"]:
        source_node_ids.append(row["summary_of"])
    summary_source = row["source"]
    if isinstance(summary_source, str) and summary_source:
        summary_source = json.loads(summary_source)
    if isinstance(summary_source, dict):
        source_node_ids.extend(summary_source.get("source_node_ids") or [])
    if row["object_type"] == "entity":
        fact_rows = await database.database.fetch_all(
            "SELECT article_id FROM entity_facts WHERE entity_id = :id AND article_id IS NOT NULL",
            {"id": node_id},
        )
        source_node_ids.extend(r["article_id"] for r in fact_rows)
    source_node_ids = list(dict.fromkeys(source_node_ids))

    rows = await database.database.fetch_all(
        """
        SELECT n.id AS node_id, n.title AS node_title, n.object_type,
               COALESCE(an.source_type, n.object_type) AS source_type,
               an.raw_ref, an.source_item_id,
               si.origin_ref, si.origin_ref_type, si.raw_snapshot_ref,
               si.extracted_text_ref, si.source_published_at,
               s.id AS source_id, s.name AS source_name, s.type AS configured_source_type
        FROM knowledge_nodes n
        LEFT JOIN article_nodes an ON an.node_id = n.id
        LEFT JOIN source_items si ON si.id = an.source_item_id
        LEFT JOIN sources s ON s.id = COALESCE(si.source_id, n.source_id)
        WHERE n.user_id = :user_id AND n.id = ANY(:source_node_ids)
        ORDER BY n.created_at DESC
        """,
        {"user_id": user_id, "source_node_ids": source_node_ids},
    )
    sources = []
    refs = []
    for r in rows:
        item = dict(r)
        if item.get("source_published_at"):
            item["source_published_at"] = item["source_published_at"].isoformat()
        if item.get("raw_ref") and isinstance(item["raw_ref"], str):
            item["raw_ref"] = json.loads(item["raw_ref"])
        sources.append(item)
        refs.append(
            {
                "id": item["node_id"],
                "title": item["node_title"],
                "object_type": item["object_type"],
                "source_type": item["source_type"],
            }
        )
    return {"tool": "kb_get_sources", "sources": sources, "references": refs}


async def run_tool(name: str, tool_input: dict[str, Any], user_id: str = USER_ID) -> dict[str, Any]:
    if name == "kb_search":
        return await search(str(tool_input.get("query") or ""), tool_input, user_id)
    if name == "kb_get_node":
        return await get_node(str(tool_input.get("id") or ""), user_id)
    if name == "kb_get_neighbors":
        return await get_neighbors(str(tool_input.get("id") or ""), int(tool_input.get("depth") or 1), user_id)
    if name == "kb_get_sources":
        return await get_sources(str(tool_input.get("node_id") or ""), user_id)
    return {"tool": name, "error": "unknown read-only tool", "references": []}

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

KB_API_BASE = os.environ.get("KB_API_BASE", "http://api:8000").rstrip("/")

mcp = FastMCP("knowledgebase")

_client = httpx.AsyncClient(base_url=KB_API_BASE, timeout=30.0)


@mcp.tool()
async def kb_search(
    query: str,
    limit: int = 10,
    tags: list[str] | None = None,
    type: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search over the personal knowledge base.

    Returns ranked nodes by vector similarity (OpenAI text-embedding-3-small
    against pgvector). Each result has id, title, abstract, tags, object_type,
    score, source_type, created_at.

    Args:
        query: natural-language question or keyword query.
        limit: max results, 1-50. Default 10.
        tags: optional list of tag strings to filter by (AND with query).
        type: optional 'article' | 'entity' | 'summary' to filter by object type.
    """
    params: dict[str, Any] = {"q": query, "limit": max(1, min(limit, 50))}
    if tags:
        params["tags"] = ",".join(tags)
    if type:
        params["type"] = type
    r = await _client.get("/api/kb/search", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_get_node(node_id: str) -> dict[str, Any]:
    """Fetch a single knowledge-base node by id, including its neighbors.

    The response includes the node body/abstract and an `edges` array listing
    every relation (from_node_id, to_node_id, edge_type). Use this to traverse
    the graph: there is no separate neighbors endpoint — neighbors come
    embedded in this payload.
    """
    r = await _client.get(f"/api/kb/node/{node_id}")
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_current_time() -> dict[str, str]:
    """Return the current date and time in UTC.

    Use this whenever the user asks about today's date, the current time, or
    anything time-sensitive (e.g. "what's new this week", "how old is X").
    The container clock is UTC; convert locally if the user asks for another
    timezone.
    """
    now = datetime.now(timezone.utc)
    return {
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time_utc": now.strftime("%H:%M:%S UTC"),
        "weekday": now.strftime("%A"),
    }


@mcp.tool()
async def kb_get_ancestors(object_id: str) -> dict[str, Any]:
    """Return the index/folder ancestor chain for a node.

    Useful for breadcrumb context — shows where the node sits in the user's
    knowledge-base index hierarchy.
    """
    r = await _client.get(f"/api/kb/objects/{object_id}/ancestors")
    r.raise_for_status()
    return r.json()

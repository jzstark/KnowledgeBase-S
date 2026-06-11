import os
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

try:
    from mcp.server.transport_security import TransportSecuritySettings

    _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
except ImportError:
    _security = None

KB_API_BASE = os.environ.get("KB_API_BASE", "https://swanny.laughtale.co.uk").rstrip("/")
KB_PUBLIC_PREFIX = os.environ.get("KB_PUBLIC_PREFIX", "/api/kb/v1").rstrip("/")
KB_SERVICE_TOKEN = os.environ.get("KB_SERVICE_TOKEN", "").strip()

mcp = FastMCP("knowledgebase", transport_security=_security) if _security else FastMCP("knowledgebase")

NodeType = Literal["article", "entity", "summary", "index"]
Relation = Literal["mentions", "mentioned_by", "summarizes", "summarized_by", "contains", "part_of"]
OutputFormat = Literal["bullet", "prose", "structured"]


def _headers() -> dict[str, str]:
    if not KB_SERVICE_TOKEN:
        return {}
    return {
        "Authorization": f"Bearer {KB_SERVICE_TOKEN}",
        "X-KB-Service-Token": KB_SERVICE_TOKEN,
    }


def _csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(v for v in values if v)


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None and v != ""}


def _clean_json(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v is not None}


def _path(path: str) -> str:
    return f"{KB_PUBLIC_PREFIX}{path}"


_client = httpx.AsyncClient(base_url=KB_API_BASE, headers=_headers(), timeout=60.0)


@mcp.tool()
async def kb_search(
    query: str,
    limit: int = 10,
    include_snippet: bool = True,
    type: NodeType | None = None,
    tags: list[str] | None = None,
    source_ids: list[str] | None = None,
    doc_kind: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Search the knowledge base with vector + keyword matching.

    Use this for finding relevant nodes. Filters are optional. date_from and
    date_to must be ISO dates, for example 2026-05-28.
    """
    params = _clean_params(
        {
            "query": query,
            "top_k": max(1, min(int(limit), 50)),
            "include_snippet": include_snippet,
            "type": type,
            "tags": _csv(tags),
            "source_ids": _csv(source_ids),
            "doc_kind": _csv(doc_kind),
            "date_from": date_from,
            "date_to": date_to,
        }
    )
    r = await _client.get(_path("/search"), params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_get_node(
    node_id: str,
    include_body: bool = True,
    include_related_ids: bool = False,
) -> dict[str, Any]:
    """Fetch one knowledge-base node, including summaries and index outline."""
    r = await _client.get(
        _path(f"/nodes/{node_id}"),
        params={"include_body": include_body, "include_related_ids": include_related_ids},
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_get_nodes_batch(
    ids: list[str],
    include_body: bool = True,
    include_related_ids: bool = False,
) -> dict[str, Any]:
    """Fetch multiple knowledge-base nodes in one request."""
    r = await _client.post(
        _path("/nodes/batch"),
        json={"ids": ids, "include_body": include_body, "include_related_ids": include_related_ids},
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_get_related(
    node_id: str,
    relation: Relation,
    limit: int = 20,
) -> dict[str, Any]:
    """Navigate graph relations from one node."""
    r = await _client.get(
        _path(f"/nodes/{node_id}/related"),
        params={"relation": relation, "limit": max(1, min(int(limit), 100))},
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_timeline(
    entity_id: str | None = None,
    topic_query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    include_facts: bool = False,
) -> dict[str, Any]:
    """Build a timeline for an entity id or a topic query."""
    params = _clean_params(
        {
            "entity_id": entity_id,
            "topic_query": topic_query,
            "date_from": date_from,
            "date_to": date_to,
            "limit": max(1, min(int(limit), 200)),
            "include_facts": include_facts,
        }
    )
    r = await _client.get(_path("/timeline"), params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_compare(
    node_ids: list[str],
    dimensions: list[str] | None = None,
    focus: str | None = None,
) -> dict[str, Any]:
    """Compare 2 to 5 nodes and return a comparison table plus analysis."""
    r = await _client.post(
        _path("/compare"),
        json=_clean_json({"node_ids": node_ids, "dimensions": dimensions, "focus": focus}),
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_cite(
    claim: str,
    context: str | None = None,
    doc_kinds: list[str] | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    """Find source quotes that support or refute a claim.

    The KnowledgeBase API verifies returned quotes against the article text.
    """
    r = await _client.post(
        _path("/cite"),
        json=_clean_json(
            {
                "claim": claim,
                "context": context,
                "doc_kinds": doc_kinds,
                "max_results": max_results,
            }
        ),
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def kb_summarize_corpus(
    node_ids: list[str] | None = None,
    query: str | None = None,
    max_sources: int | None = None,
    focus: str | None = None,
    output_format: OutputFormat = "prose",
) -> dict[str, Any]:
    """Summarize a corpus selected by explicit node ids or by a query."""
    r = await _client.post(
        _path("/summarize_corpus"),
        json=_clean_json(
            {
                "node_ids": node_ids,
                "query": query,
                "max_sources": max_sources,
                "focus": focus,
                "output_format": output_format,
            }
        ),
    )
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_current_time() -> dict[str, str]:
    """Return the current date and time in UTC."""
    now = datetime.now(timezone.utc)
    return {
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time_utc": now.strftime("%H:%M:%S UTC"),
        "weekday": now.strftime("%A"),
    }

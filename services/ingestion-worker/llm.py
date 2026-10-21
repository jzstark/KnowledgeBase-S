"""Model calls for ingestion, batched by the API (POST /api/kb/llm/call).

Each source item owns its calls. A call whose result is still in a batch raises
``Deferred``; the pipeline puts the item back to pending and replays it later,
when finished calls return their stored result. Keys must stay stable across
replays because the first recorded request wins.
"""

import os
from typing import Any

import httpx


class Deferred(Exception):
    """A model result for this source item is still being batched."""


def call(source_item_id: str, key: str, params: dict[str, Any]):
    from anthropic.types import Message

    token = os.environ.get("KB_SERVICE_TOKEN", "").strip()
    resp = httpx.post(
        f"{os.environ['API_BASE_URL']}/api/kb/llm/call",
        json={"owner": f"source_item:{source_item_id}", "key": key, "params": params},
        headers={"X-KB-Service-Token": token} if token else {},
        timeout=300,
    )
    if resp.status_code == 202:
        raise Deferred
    resp.raise_for_status()
    return Message.model_validate(resp.json()["message"])


def call_many(source_item_id: str, calls: list[tuple[str, dict[str, Any]]]) -> list:
    """Record every call before deferring, so independent calls share one batch round."""
    messages = []
    deferred = False
    for key, params in calls:
        try:
            messages.append(call(source_item_id, key, params))
        except Deferred:
            deferred = True
    if deferred:
        raise Deferred
    return messages

"""
Entity CRUD — update, merge, delete, and query entity nodes.

Routes:
  GET    /api/kb/entities/{entity_id}/facts
  GET    /api/kb/entities/{entity_id}/timeline
  GET    /api/kb/entities/{entity_id}/related
  POST   /api/kb/entities/{entity_id}/regenerate
  PATCH  /api/kb/entities/{entity_id}
  POST   /api/kb/entities/merge
  DELETE /api/kb/entities/{entity_id}
  GET    /api/kb/entity_candidates
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import database
from kb.graph import refresh_entity_profile
from auth import require_auth
from kb.common import USER_ID
from kb.wiki import _wiki_file_path

router = APIRouter(prefix="/api/kb", tags=["KB Internal"])


# ── Models ────────────────────────────────────────────────────────────────────

class UpdateEntityRequest(BaseModel):
    canonical_name: str | None = None
    aliases: list[str] | None = None
    entity_type: str | None = None


class MergeEntitiesRequest(BaseModel):
    source_id: str  # merged away (becomes tombstone)
    target_id: str  # survives


# ── Domain functions ──────────────────────────────────────────────────────────

async def do_update_entity(entity_id: str, body: UpdateEntityRequest) -> None:
    """Apply canonical_name / aliases / entity_type updates to an entity node."""
    row = await database.database.fetch_one(
        "SELECT object_type FROM knowledge_nodes WHERE id = :id", {"id": entity_id},
    )
    if not row or row["object_type"] != "entity":
        raise ValueError("entity 不存在")

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
            f"UPDATE entity_nodes SET {', '.join(entity_updates)} WHERE node_id = :id", params,
        )
    if body.canonical_name is not None:
        await database.database.execute(
            "UPDATE knowledge_nodes SET title = :name, updated_at = NOW() WHERE id = :id",
            {"name": body.canonical_name.strip(), "id": entity_id},
        )


async def do_merge_entities(source_id: str, target_id: str) -> None:
    """
    Merge source entity into target. Edges and entity_facts are transferred;
    source is kept as a tombstone with merged_into pointing at target.
    """
    if source_id == target_id:
        raise ValueError("source 和 target 不能相同")

    for eid in (source_id, target_id):
        row = await database.database.fetch_one(
            "SELECT object_type FROM knowledge_nodes WHERE id = :id", {"id": eid},
        )
        if not row or row["object_type"] != "entity":
            raise ValueError(f"entity 不存在：{eid}")

    # Transfer non-conflicting edges (source as from-node)
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
    # Transfer non-conflicting edges (source as to-node)
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
    # Drop remaining duplicate edges still referencing source
    await database.database.execute(
        "DELETE FROM knowledge_edges WHERE from_node_id = :source OR to_node_id = :source",
        {"source": source_id},
    )
    await database.database.execute(
        "UPDATE entity_facts SET entity_id = :target WHERE entity_id = :source",
        {"source": source_id, "target": target_id},
    )
    await database.database.execute(
        "UPDATE entity_nodes SET merged_into = :target WHERE node_id = :source",
        {"source": source_id, "target": target_id},
    )


async def do_delete_entity(entity_id: str) -> None:
    """Hard-delete an entity node: wiki file, facts, edges, entity_nodes, knowledge_nodes."""
    row = await database.database.fetch_one(
        "SELECT user_id, object_type FROM knowledge_nodes WHERE id = :id", {"id": entity_id},
    )
    if not row or row["object_type"] != "entity":
        raise ValueError("entity 不存在")

    uid = row["user_id"] or USER_ID
    wiki_file = _wiki_file_path(uid, entity_id, "entity")
    if wiki_file.exists():
        wiki_file.unlink()

    for stmt, params in [
        ("DELETE FROM entity_facts WHERE entity_id = :id", {"id": entity_id}),
        ("DELETE FROM knowledge_edges WHERE from_node_id = :id OR to_node_id = :id", {"id": entity_id}),
        ("DELETE FROM entity_nodes WHERE node_id = :id", {"id": entity_id}),
        ("DELETE FROM knowledge_nodes WHERE id = :id", {"id": entity_id}),
    ]:
        await database.database.execute(stmt, params)


# ── Shared query ──────────────────────────────────────────────────────────────

async def _fetch_entity_facts(entity_id: str, limit: int) -> list[dict]:
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
            "source_published_at": r["source_published_at"].isoformat() if r["source_published_at"] else None,
            "evidence_span": r["evidence_span"],
            "confidence": float(r["confidence"] or 0),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]


# ── Route handlers ────────────────────────────────────────────────────────────

@router.get("/entities/{entity_id}/facts")
async def list_entity_facts(entity_id: str, limit: int = Query(50, ge=1, le=200)):
    return await _fetch_entity_facts(entity_id, limit)


@router.get("/entities/{entity_id}/timeline")
async def get_entity_timeline(entity_id: str, limit: int = Query(50, ge=1, le=200)):
    facts = await _fetch_entity_facts(entity_id, limit)
    abstract_row = await database.database.fetch_one(
        "SELECT abstract, updated_at FROM knowledge_nodes WHERE id = :id", {"id": entity_id},
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
    result = await refresh_entity_profile(entity_id)
    if not result.get("refreshed"):
        raise HTTPException(404, "entity 不存在")
    return result


@router.patch("/entities/{entity_id}")
async def update_entity(entity_id: str, body: UpdateEntityRequest, _: dict = Depends(require_auth)):
    """Update canonical_name, aliases, or entity_type."""
    try:
        await do_update_entity(entity_id, body)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.post("/entities/merge")
async def merge_entities(body: MergeEntitiesRequest, _: dict = Depends(require_auth)):
    """Merge source entity into target; source becomes a tombstone."""
    try:
        await do_merge_entities(body.source_id, body.target_id)
    except ValueError as e:
        msg = str(e)
        raise HTTPException(400 if "相同" in msg else 404, msg)
    return {"ok": True, "source_id": body.source_id, "target_id": body.target_id}


@router.delete("/entities/{entity_id}")
async def delete_entity(entity_id: str, _: dict = Depends(require_auth)):
    """Hard-delete an entity (facts, edges, DB rows, wiki file)."""
    try:
        await do_delete_entity(entity_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.get("/entity_candidates")
async def list_entity_candidates(_: dict = Depends(require_auth)):
    """List unpromoted entity candidates ordered by mention count. For debugging."""
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

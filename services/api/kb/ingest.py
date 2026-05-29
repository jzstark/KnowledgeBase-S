"""
Ingestion pipeline — the write path for new knowledge nodes.

Routes (called by ingestion-worker, no auth required):
  POST /api/kb/ingest
  POST /api/kb/entity_candidates/analyze_context
  POST /api/kb/entity_candidates/process
  POST /api/kb/entity_candidates/{candidate_id}/mark_promoted
  POST /api/kb/entities/{entity_id}/backfill_wikilinks
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

import database
from settings import settings
from kb.common import USER_DATA_DIR, USER_ID, _vector_literal
from kb.graph import (
    add_child,
    upsert_object_node,
    upsert_fact_from_mention,
    refresh_entity_profile,
)
from kb.retrieval import _embed_text
from kb.wiki import write_wiki_node

RAW_CAP_BYTES = 512 * 1024 * 1024  # 512 MB

router = APIRouter(prefix="/api/kb", tags=["KB Internal"])


# ── Models ────────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    user_id: str = "default"
    title: str | None = None
    abstract: str
    embedding: list[float]
    source_type: str
    source_id: str
    raw_ref: dict[str, Any] = {}
    tags: list[str] = []
    object_type: str = "article"
    source_node_ids: list[str] = []
    summary_of: str | None = None
    canonical_name: str | None = None
    aliases: list[str] = []
    perspective: str | None = None
    perspective_label: str | None = None
    perspective_instruction: str | None = None
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    captured_at: datetime | None = None
    effective_at: datetime | None = None
    source_item_id: str | None = None
    document_instance_id: str | None = None   # Phase B: 稳定身份键
    parent_index_id: str | None = None
    doc_kind: str | None = None
    embedding_model: str | None = None


class EntityCandidateItem(BaseModel):
    name: str
    aliases: list[str] = []
    salience: float
    matches_existing_entity_id: str | None = None
    summary_hint: str = ""


class ProcessCandidatesRequest(BaseModel):
    article_id: str
    entities: list[EntityCandidateItem]


# ── Utilities ─────────────────────────────────────────────────────────────────

def _make_node_id(
    object_type: str,
    raw_ref: dict,
    user_id: str,
    canonical_name: str | None,
    summary_of: str | None = None,
    perspective_label: str | None = None,
    perspective_instruction: str | None = None,
    document_instance_id: str | None = None,
) -> str:
    """Deterministic ID for source-backed nodes and entities; random otherwise.
    Priority: document_instance_id > raw_ref.path > raw_ref.url > entity/summary keys > random.
    """
    prefix = object_type[:3] if object_type else "nod"
    # Phase B: document_instance_id 是最稳定的身份键，优先使用
    if document_instance_id and object_type == "article":
        h = hashlib.sha256(f"{user_id}:{document_instance_id}".encode()).hexdigest()[:16]
        return f"art_{h}"
    raw_path = (raw_ref or {}).get("path")
    if raw_path:
        h = hashlib.sha256(raw_path.encode()).hexdigest()[:16]
        return f"{prefix}_{h}"
    raw_url = (raw_ref or {}).get("url")
    if raw_url:
        h = hashlib.sha256(raw_url.encode()).hexdigest()[:16]
        return f"{prefix}_{h}"
    if object_type == "summary" and summary_of:
        h = hashlib.sha256(
            f"{user_id}:{summary_of}:{perspective_label or ''}:{perspective_instruction or ''}".encode()
        ).hexdigest()[:16]
        return f"sum_{h}"
    if object_type == "entity" and canonical_name:
        h = hashlib.sha256(f"{user_id}:{canonical_name}".encode()).hexdigest()[:16]
        return f"ent_{h}"
    return f"{prefix}_{secrets.token_hex(6)}"


def _summary_perspective(
    label: str | None,
    instruction: str | None,
    legacy_perspective: str | None = None,
) -> tuple[str, str, bool]:
    label = (label or legacy_perspective or "").strip()
    instruction = (instruction or legacy_perspective or "").strip()
    is_default = not label and not instruction
    return label or "default", instruction or "默认摘要", is_default


# ── Domain functions ──────────────────────────────────────────────────────────

async def do_ingest(body: IngestRequest) -> str | dict:
    """
    Core ingest logic: dedup checks, doc_kind cascade, node creation.
    Returns node_id on success, or a dict with {id, duplicate: True} if skipped.
    """
    # Phase B: document_instance_id 去重（优先）
    if body.document_instance_id and body.object_type == "article":
        existing = await database.database.fetch_one(
            """
            SELECT n.id FROM knowledge_nodes n
            JOIN article_nodes an ON an.node_id = n.id
            WHERE n.user_id = :uid AND an.document_instance_id = :di_id
            """,
            {"uid": body.user_id, "di_id": body.document_instance_id},
        )
        if existing:
            return {"id": existing["id"], "duplicate": True}

    raw_path = (body.raw_ref or {}).get("path")
    if raw_path:
        existing = await database.database.fetch_one(
            """
            SELECT n.id FROM knowledge_nodes n
            JOIN article_nodes an ON an.node_id = n.id
            WHERE n.user_id = :uid AND an.raw_ref->>'path' = :path
            """,
            {"uid": body.user_id, "path": raw_path},
        )
        if existing:
            return {"id": existing["id"], "duplicate": True}

    raw_url = (body.raw_ref or {}).get("url")
    if raw_url:
        existing = await database.database.fetch_one(
            """
            SELECT n.id FROM knowledge_nodes n
            JOIN article_nodes an ON an.node_id = n.id
            WHERE n.user_id = :uid AND an.raw_ref->>'url' = :url
            """,
            {"uid": body.user_id, "url": raw_url},
        )
        if existing:
            return {"id": existing["id"], "duplicate": True}

    if body.object_type == "entity" and body.canonical_name:
        existing_ent = await database.database.fetch_one(
            """
            SELECT n.id FROM knowledge_nodes n
            JOIN entity_nodes en ON en.node_id = n.id
            WHERE n.user_id = :uid AND n.object_type = 'entity'
              AND en.canonical_name = :name
            """,
            {"uid": body.user_id, "name": body.canonical_name},
        )
        if existing_ent:
            return {"id": existing_ent["id"], "duplicate": True}

    embedding_literal = _vector_literal(body.embedding)
    perspective_label = None
    perspective_instruction = None
    is_default = False
    body_embedding_literal = None
    perspective_embedding_literal = None

    # doc_kind cascade:
    #   显式 > document_instances.doc_kind > source_items.doc_kind
    #   > sources.default_doc_kind > config.doc_kind.default
    doc_kind = (body.doc_kind or "").strip() or None
    if not doc_kind and body.document_instance_id:
        di_row = await database.database.fetch_one(
            "SELECT doc_kind FROM document_instances WHERE id = :id",
            {"id": body.document_instance_id},
        )
        if di_row and di_row["doc_kind"]:
            doc_kind = di_row["doc_kind"]
    if not doc_kind and body.source_item_id:
        si_row = await database.database.fetch_one(
            "SELECT doc_kind FROM source_items WHERE id = :id",
            {"id": body.source_item_id},
        )
        if si_row and si_row["doc_kind"]:
            doc_kind = si_row["doc_kind"]
    if not doc_kind and body.source_id:
        s_row = await database.database.fetch_one(
            "SELECT default_doc_kind FROM sources WHERE id = :id",
            {"id": body.source_id},
        )
        if s_row and s_row["default_doc_kind"]:
            doc_kind = s_row["default_doc_kind"]
    if not doc_kind:
        doc_kind = settings.doc_kind.default
    allowed_kinds = set(settings.doc_kind.values)
    if allowed_kinds and doc_kind not in allowed_kinds:
        doc_kind = settings.doc_kind.default

    embedding_model = body.embedding_model or settings.embedding.model

    if body.object_type == "summary":
        perspective_label, perspective_instruction, is_default = _summary_perspective(
            body.perspective_label,
            body.perspective_instruction,
            body.perspective,
        )
        body_embedding_literal = embedding_literal
        perspective_embedding = await _embed_text(f"{perspective_label}\n{perspective_instruction}")
        perspective_embedding_literal = _vector_literal(perspective_embedding)

    node_id = _make_node_id(
        body.object_type,
        body.raw_ref or {},
        body.user_id,
        body.canonical_name,
        body.summary_of,
        perspective_label,
        perspective_instruction,
        body.document_instance_id,
    )

    if body.object_type == "summary" and body.summary_of:
        existing_summary = await database.database.fetch_one(
            "SELECT id FROM knowledge_nodes WHERE user_id = :uid AND id = :id",
            {"uid": body.user_id, "id": node_id},
        )
        if existing_summary:
            return {"id": existing_summary["id"], "duplicate": True}

    captured_at = body.captured_at or datetime.now(timezone.utc)
    published_at = body.effective_at or body.source_published_at or captured_at

    await database.database.execute(
        f"""
        INSERT INTO knowledge_nodes
          (id, user_id, title, abstract, embedding, source_id,
           tags, object_type, published_at, doc_kind, embedding_model)
        VALUES
          (:id, :user_id, :title, :abstract, '{embedding_literal}'::vector,
           :source_id, :tags, :object_type, :published_at, :doc_kind, :embedding_model)
        """,
        {
            "id": node_id,
            "user_id": body.user_id,
            "title": body.title,
            "abstract": body.abstract,
            "source_id": body.source_id,
            "tags": body.tags,
            "object_type": body.object_type,
            "published_at": published_at,
            "doc_kind": doc_kind,
            "embedding_model": embedding_model,
        },
    )
    await upsert_object_node(
        node_id,
        body.object_type,
        {
            "source_item_id": body.source_item_id,
            "document_instance_id": body.document_instance_id,
            "raw_ref": body.raw_ref,
            "source_type": body.source_type,
            "source_published_at": body.source_published_at,
            "source_updated_at": body.source_updated_at,
            "captured_at": captured_at,
            "effective_at": body.effective_at,
            "tags": body.tags,
            "summary_of": body.summary_of,
            "perspective_label": perspective_label,
            "perspective_instruction": perspective_instruction,
            "body": body.abstract,
            "body_embedding_literal": body_embedding_literal,
            "perspective_embedding_literal": perspective_embedding_literal,
            "is_default": is_default,
            "source": {"source_node_ids": body.source_node_ids},
            "canonical_name": body.canonical_name,
            "aliases": body.aliases,
            "description": body.abstract,
        },
    )
    if body.parent_index_id:
        await add_child(
            body.parent_index_id,
            node_id,
            user_id=body.user_id,
            child_role="chapter" if body.source_type == "book_chapter" else "member",
        )

    return node_id


async def build_similar_edges(node_id: str, user_id: str) -> None:
    """Find cosine-similar nodes above threshold and create similar_to edges."""
    limit = settings.retrieval.similar_to_limit
    threshold = settings.retrieval.similar_to_threshold
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


async def build_similar_edges_and_wiki(node_id: str, user_id: str) -> None:
    await build_similar_edges(node_id, user_id)
    await write_wiki_node(node_id, user_id)


def trim_raw_files(user_id: str) -> None:
    """Delete oldest raw files when the raw/ directory exceeds RAW_CAP_BYTES."""
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


async def do_entity_analyze_context(embedding: list[float]) -> dict:
    """Return nearby entities, top candidates, and popular tags for article analysis."""
    embedding_literal = "[" + ",".join(repr(x) for x in embedding) + "]"
    nearby_limit = settings.ingestion.context_nearby_entities
    candidate_limit = settings.ingestion.context_top_candidates
    tags_limit = settings.ingestion.context_popular_tags

    async with database.database.connection() as conn:
        entity_rows = await conn.raw_connection.fetch(
            f"""
            SELECT n.id, n.title, COALESCE(en.canonical_name, n.title) AS canonical_name
            FROM knowledge_nodes n
            LEFT JOIN entity_nodes en ON en.node_id = n.id
            WHERE n.user_id = $1 AND n.object_type = 'entity' AND n.embedding IS NOT NULL
            ORDER BY n.embedding <=> '{embedding_literal}'::vector
            LIMIT {nearby_limit}
            """,
            USER_ID,
        )
        candidate_rows = await conn.raw_connection.fetch(
            f"""
            SELECT id, canonical_name, aliases, mention_count, max_salience
            FROM entity_candidates
            WHERE user_id = $1 AND promoted_entity_id IS NULL
            ORDER BY mention_count DESC, max_salience DESC
            LIMIT {candidate_limit}
            """,
            USER_ID,
        )
        tag_rows = await conn.raw_connection.fetch(
            f"""
            SELECT tag, COUNT(*) AS freq
            FROM knowledge_nodes, unnest(tags) AS tag
            WHERE user_id = $1 AND tags IS NOT NULL
            GROUP BY tag
            ORDER BY freq DESC, tag ASC
            LIMIT {tags_limit}
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
        "popular_tags": [
            {"tag": r["tag"], "freq": int(r["freq"])} for r in tag_rows
        ],
    }


async def do_process_entity_candidates(body: ProcessCandidatesRequest) -> dict:
    """
    Upsert entity candidates from ingestion-worker and check promotion thresholds.
    Returns matched_existing entity IDs and a list of candidates ready to promote.
    """
    matched_existing: list[str] = []
    promoted: list[dict] = []

    for ent in body.entities:
        if ent.matches_existing_entity_id:
            await upsert_fact_from_mention(
                ent.matches_existing_entity_id,
                body.article_id,
                summary_hint=ent.summary_hint,
                salience=ent.salience,
                user_id=USER_ID,
            )
            await database.database.execute(
                "UPDATE entity_nodes SET abstract_stale = true, updated_at = NOW() WHERE node_id = :id",
                {"id": ent.matches_existing_entity_id},
            )
            matched_existing.append(ent.matches_existing_entity_id)
            continue

        existing_cand = await database.database.fetch_one(
            """
            SELECT id, source_article_ids
            FROM entity_candidates
            WHERE user_id = :uid AND canonical_name = :name
            """,
            {"uid": USER_ID, "name": ent.name},
        )

        if existing_cand:
            cand_id = existing_cand["id"]
            existing_article_ids = list(existing_cand["source_article_ids"] or [])
            if body.article_id not in existing_article_ids:
                await database.database.execute(
                    """
                    UPDATE entity_candidates
                    SET source_article_ids = array_append(
                            COALESCE(source_article_ids, '{}'), :article_id),
                        mention_count = COALESCE(mention_count, 0) + 1,
                        max_salience = GREATEST(COALESCE(max_salience, 0), :salience),
                        aliases = (
                            SELECT array(SELECT DISTINCT unnest(aliases || CAST(:new_aliases AS text[])))
                        ),
                        updated_at = NOW()
                    WHERE id = :cid
                    """,
                    {
                        "cid": cand_id,
                        "article_id": body.article_id,
                        "new_aliases": ent.aliases,
                        "salience": float(ent.salience),
                    },
                )
        else:
            await database.database.execute(
                """
                INSERT INTO entity_candidates
                  (user_id, canonical_name, aliases, source_article_ids,
                   mention_count, max_salience)
                VALUES (:uid, :name, :aliases, ARRAY[:article_id]::text[], 1, :salience)
                """,
                {
                    "uid": USER_ID,
                    "name": ent.name,
                    "aliases": ent.aliases,
                    "article_id": body.article_id,
                    "salience": float(ent.salience),
                },
            )
            cand_id_row = await database.database.fetch_one(
                "SELECT id FROM entity_candidates WHERE user_id = :uid AND canonical_name = :name",
                {"uid": USER_ID, "name": ent.name},
            )
            cand_id = cand_id_row["id"] if cand_id_row else None

        if cand_id is None:
            continue

        cand_row = await database.database.fetch_one(
            """
            SELECT id, canonical_name, aliases, source_article_ids,
                   promoted_entity_id, mention_count, max_salience
            FROM entity_candidates WHERE id = :cid
            """,
            {"cid": cand_id},
        )
        if not cand_row or cand_row["promoted_entity_id"]:
            continue

        mention_count = int(cand_row["mention_count"] or 0)
        max_salience = float(cand_row["max_salience"] or 0)
        source_article_ids = list(cand_row["source_article_ids"] or [])

        should_promote = (
            max_salience >= settings.entity.promotion_max_salience
            or (max_salience >= settings.entity.promotion_salience
                and mention_count >= settings.entity.promotion_salience_mentions)
            or mention_count >= settings.entity.promotion_min_mentions
        )
        if should_promote:
            promoted.append({
                "candidate_id": cand_row["id"],
                "canonical_name": cand_row["canonical_name"],
                "aliases": list(cand_row["aliases"]) if cand_row["aliases"] else [],
                "source_article_ids": source_article_ids,
                "summary_hint": ent.summary_hint,
            })

    return {"matched_existing": matched_existing, "promoted": promoted}


async def _materialize_candidate_facts(candidate_id: int, entity_node_id: str) -> dict:
    """Back-fill entity_facts when a candidate is promoted."""
    cand = await database.database.fetch_one(
        """
        SELECT canonical_name, source_article_ids, max_salience
        FROM entity_candidates WHERE id = :cid
        """,
        {"cid": candidate_id},
    )
    if not cand:
        return {"facts_inserted": 0}
    article_ids = list(cand["source_article_ids"] or [])
    fallback_salience = float(cand["max_salience"] or 0.5) or 0.5
    inserted = 0
    for article_id in article_ids:
        if not article_id:
            continue
        created = await upsert_fact_from_mention(
            entity_node_id,
            article_id,
            canonical_name=cand["canonical_name"],
            summary_hint=None,
            salience=fallback_salience,
            user_id=USER_ID,
        )
        if created:
            inserted += 1
    return {"facts_inserted": inserted}


# ── Route handlers ────────────────────────────────────────────────────────────

@router.post("/ingest")
async def ingest_endpoint(body: IngestRequest, background_tasks: BackgroundTasks):
    """Content ingest entry point — called by ingestion-worker, no auth required."""
    result = await do_ingest(body)
    if isinstance(result, dict):
        return result
    node_id = result
    background_tasks.add_task(build_similar_edges_and_wiki, node_id, body.user_id)
    if body.raw_ref:
        background_tasks.add_task(trim_raw_files, body.user_id)
    return {"id": node_id}


@router.post("/entity_candidates/analyze_context")
async def entity_analyze_context(body: dict):
    """Return analysis context for ingestion-worker: nearby entities, top candidates, popular tags."""
    embedding = body.get("embedding", [])
    if not embedding:
        return {"nearby_entities": [], "top_candidates": [], "popular_tags": []}
    return await do_entity_analyze_context(embedding)


@router.post("/entity_candidates/process")
async def process_entity_candidates(body: ProcessCandidatesRequest):
    """Process entity candidates submitted by ingestion-worker; return which are ready to promote."""
    return await do_process_entity_candidates(body)


@router.post("/entity_candidates/{candidate_id}/mark_promoted")
async def mark_candidate_promoted(candidate_id: int, body: dict):
    """Called by ingestion-worker after generating an entity page to mark the candidate promoted."""
    entity_node_id = body.get("entity_node_id")
    if not entity_node_id:
        raise HTTPException(400, "entity_node_id 必填")
    await database.database.execute(
        "UPDATE entity_candidates SET promoted_entity_id = :eid WHERE id = :cid",
        {"eid": entity_node_id, "cid": candidate_id},
    )
    facts_result = await _materialize_candidate_facts(candidate_id, entity_node_id)
    await refresh_entity_profile(entity_node_id)
    return {"ok": True, **facts_result}


@router.post("/entities/{entity_id}/backfill_wikilinks")
async def backfill_entity_wikilinks(entity_id: str):
    """Re-scan all article bodies and inject wikilinks for a newly promoted entity."""
    from maintenance import backfill_wikilinks_for_entity
    result = await backfill_wikilinks_for_entity(entity_id, USER_ID)
    return result

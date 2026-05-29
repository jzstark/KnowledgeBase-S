"""
Knowledge graph persistence — all node, edge, hierarchy, and entity-fact operations.

Consolidated from object_nodes.py, index_structure.py, entity_insights.py.

Public surface:
  Node upsert / fetch
    upsert_object_node(node_id, object_type, fields)
    fetch_object_fields(node_id, object_type)
    fetch_node_with_object_fields(node_id)
    fetch_source_node_ids(node_id, object_type)

  Index hierarchy
    mark_index_stale(index_id)
    add_child(index_id, child_id, ...)
    remove_child(index_id, child_id, user_id)
    reorder_children(index_id, child_ids, user_id)
    get_children(index_id, user_id)
    get_parents(object_id, user_id)
    get_ancestors(object_id, user_id)
    get_descendants(index_id, user_id)

  Entity facts and signals
    upsert_entity_fact(entity_id, article_id, fact_text, ...)
    upsert_fact_from_mention(entity_id, article_id, ...)
    backfill_entity_facts_from_mentions(user_id)
    refresh_entity_profile(entity_id)          -- 确定性拼接，无 LLM（保留用于兜底）
    lm_refresh_entity_abstract(entity_id)      -- LLM 更新 abstract + embedding
    refresh_stale_entity_abstracts(user_id)    -- 批量刷新 abstract_stale=true 的 entity
    rebuild_entity_pair_signals(user_id)
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any

import database
from prompts import prompts
from settings import settings

logger = logging.getLogger(__name__)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _ordered_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


# ── Node upsert / fetch ────────────────────────────────────────────────────────

async def fetch_source_node_ids(node_id: str, object_type: str) -> list[str]:
    if object_type == "summary":
        row = await database.database.fetch_one(
            "SELECT summary_of, source FROM summary_nodes WHERE node_id = :node_id",
            {"node_id": node_id},
        )
        if not row:
            return []
        source = _json_dict(row["source"])
        ids = [row["summary_of"]] if row["summary_of"] else []
        ids.extend(source.get("source_node_ids") or [])
        return list(dict.fromkeys(str(i) for i in ids if i))

    if object_type == "entity":
        rows = await database.database.fetch_all(
            """
            SELECT article_id
            FROM entity_facts
            WHERE entity_id = :node_id AND article_id IS NOT NULL
            ORDER BY fact_time DESC NULLS LAST, updated_at DESC
            """,
            {"node_id": node_id},
        )
        ids = [r["article_id"] for r in rows if r["article_id"]]
        if not ids:
            edge_rows = await database.database.fetch_all(
                """
                SELECT from_node_id
                FROM knowledge_edges
                WHERE to_node_id = :node_id
                  AND relation_type IN ('mentions', 'wikilink')
                ORDER BY id DESC
                """,
                {"node_id": node_id},
            )
            ids = [r["from_node_id"] for r in edge_rows if r["from_node_id"]]
        return list(dict.fromkeys(ids))

    return []


async def upsert_object_node(node_id: str, object_type: str, fields: dict[str, Any]) -> None:
    if object_type == "article":
        await database.database.execute(
            """
            INSERT INTO article_nodes
              (node_id, source_item_id, raw_ref, source_type, source_published_at,
               source_updated_at, captured_at, effective_at, tags, status)
            VALUES
              (:node_id, :source_item_id, :raw_ref, :source_type, :source_published_at,
               :source_updated_at, :captured_at, :effective_at, :tags, :status)
            ON CONFLICT (node_id) DO UPDATE SET
              source_item_id = EXCLUDED.source_item_id,
              raw_ref = EXCLUDED.raw_ref,
              source_type = EXCLUDED.source_type,
              source_published_at = EXCLUDED.source_published_at,
              source_updated_at = EXCLUDED.source_updated_at,
              captured_at = EXCLUDED.captured_at,
              effective_at = EXCLUDED.effective_at,
              tags = EXCLUDED.tags,
              status = EXCLUDED.status,
              updated_at = NOW()
            """,
            {
                "node_id": node_id,
                "source_item_id": fields.get("source_item_id"),
                "raw_ref": database.jsonb(fields.get("raw_ref") or {}),
                "source_type": fields.get("source_type"),
                "source_published_at": fields.get("source_published_at"),
                "source_updated_at": fields.get("source_updated_at"),
                "captured_at": fields.get("captured_at"),
                "effective_at": fields.get("effective_at"),
                "tags": fields.get("tags") or [],
                "status": fields.get("status") or "active",
            },
        )
    elif object_type == "summary":
        body_embedding = fields.get("body_embedding_literal")
        perspective_embedding = fields.get("perspective_embedding_literal")
        body_embedding_sql = f"'{body_embedding}'::vector" if body_embedding else "NULL"
        perspective_embedding_sql = f"'{perspective_embedding}'::vector" if perspective_embedding else "NULL"
        await database.database.execute(
            f"""
            INSERT INTO summary_nodes
              (node_id, summary_of, perspective_label, perspective_instruction,
               perspective_embedding, body, body_embedding, is_default, source)
            VALUES
              (:node_id, :summary_of, :perspective_label, :perspective_instruction,
               {perspective_embedding_sql}, :body, {body_embedding_sql}, :is_default, :source)
            ON CONFLICT (node_id) DO UPDATE SET
              summary_of = EXCLUDED.summary_of,
              perspective_label = EXCLUDED.perspective_label,
              perspective_instruction = EXCLUDED.perspective_instruction,
              perspective_embedding = COALESCE(EXCLUDED.perspective_embedding, summary_nodes.perspective_embedding),
              body = EXCLUDED.body,
              body_embedding = COALESCE(EXCLUDED.body_embedding, summary_nodes.body_embedding),
              is_default = EXCLUDED.is_default,
              source = EXCLUDED.source,
              updated_at = NOW()
            """,
            {
                "node_id": node_id,
                "summary_of": fields.get("summary_of"),
                "perspective_label": fields.get("perspective_label"),
                "perspective_instruction": fields.get("perspective_instruction"),
                "body": fields.get("body") or fields.get("abstract") or "",
                "is_default": bool(fields.get("is_default")),
                "source": database.jsonb(fields.get("source") or {}),
            },
        )
    elif object_type == "entity":
        await database.database.execute(
            """
            INSERT INTO entity_nodes
              (node_id, canonical_name, aliases, entity_type, merged_into)
            VALUES
              (:node_id, :canonical_name, :aliases, :entity_type, :merged_into)
            ON CONFLICT (node_id) DO UPDATE SET
              canonical_name = EXCLUDED.canonical_name,
              aliases = EXCLUDED.aliases,
              entity_type = EXCLUDED.entity_type,
              merged_into = EXCLUDED.merged_into,
              updated_at = NOW()
            """,
            {
                "node_id": node_id,
                "canonical_name": fields.get("canonical_name"),
                "aliases": fields.get("aliases") or [],
                "entity_type": fields.get("entity_type"),
                "merged_into": fields.get("merged_into"),
            },
        )
    elif object_type == "index":
        await database.database.execute(
            """
            INSERT INTO index_nodes
              (node_id, description, rollup_instruction, abstract_stale)
            VALUES
              (:node_id, :description, :rollup_instruction, :abstract_stale)
            ON CONFLICT (node_id) DO UPDATE SET
              description = COALESCE(EXCLUDED.description, index_nodes.description),
              rollup_instruction = COALESCE(EXCLUDED.rollup_instruction, index_nodes.rollup_instruction),
              abstract_stale = EXCLUDED.abstract_stale,
              updated_at = NOW()
            """,
            {
                "node_id": node_id,
                "description": fields.get("description") or fields.get("abstract"),
                "rollup_instruction": fields.get("rollup_instruction"),
                "abstract_stale": bool(fields.get("abstract_stale", False)),
            },
        )


async def fetch_object_fields(node_id: str, object_type: str) -> dict[str, Any]:
    table = {
        "article": "article_nodes",
        "summary": "summary_nodes",
        "entity": "entity_nodes",
        "index": "index_nodes",
    }.get(object_type)
    if not table:
        return {}
    row = await database.database.fetch_one(
        f"SELECT * FROM {table} WHERE node_id = :node_id",
        {"node_id": node_id},
    )
    return dict(row) if row else {}


async def fetch_node_with_object_fields(node_id: str) -> dict[str, Any] | None:
    row = await database.database.fetch_one(
        "SELECT * FROM knowledge_nodes WHERE id = :id", {"id": node_id}
    )
    if not row:
        return None

    node = dict(row)
    object_type = node.get("object_type") or "article"
    extra = await fetch_object_fields(node_id, object_type)
    if not extra:
        return node

    if object_type == "article":
        for key in ("source_item_id", "raw_ref", "source_type", "source_published_at",
                    "source_updated_at", "captured_at", "effective_at", "tags"):
            if extra.get(key) is not None:
                node[key] = extra[key]
        node["article_status"] = extra.get("status")
    elif object_type == "summary":
        node["source_type"] = "summary"
        node["raw_ref"] = {}
        node["summary_of"] = extra.get("summary_of")
        node["perspective_label"] = extra.get("perspective_label")
        node["perspective_instruction"] = extra.get("perspective_instruction")
        node["is_default"] = extra.get("is_default") if extra.get("is_default") is not None else False
        if extra.get("body") is not None:
            node["abstract"] = extra["body"]
        node["summary_source"] = extra.get("source")
    elif object_type == "entity":
        node["source_type"] = "entity"
        node["raw_ref"] = {}
        node["canonical_name"] = extra.get("canonical_name") or node.get("title")
        node["aliases"] = extra.get("aliases") if extra.get("aliases") is not None else []
        node["entity_type"] = extra.get("entity_type")
        node["merged_into"] = extra.get("merged_into")
    elif object_type == "index":
        node["source_type"] = "index"
        node["raw_ref"] = {}
        node["description"] = extra.get("description")
        node["rollup_instruction"] = extra.get("rollup_instruction")
        node["abstract_stale"] = extra.get("abstract_stale")

    node["source_node_ids"] = await fetch_source_node_ids(node_id, object_type)
    return node


# ── Index hierarchy ────────────────────────────────────────────────────────────

async def mark_index_stale(index_id: str) -> None:
    await database.database.execute(
        "UPDATE index_nodes SET abstract_stale = true, updated_at = NOW() WHERE node_id = :index_id",
        {"index_id": index_id},
    )


async def _assert_index(index_id: str, user_id: str | None = None) -> dict[str, Any]:
    user_filter = "AND user_id = :user_id" if user_id is not None else ""
    row = await database.database.fetch_one(
        f"""
        SELECT id, user_id, title, object_type
        FROM knowledge_nodes
        WHERE id = :id AND object_type = 'index' {user_filter}
        """,
        {"id": index_id, "user_id": user_id},
    )
    if not row:
        raise ValueError("index not found")
    return dict(row)


async def _assert_child(child_id: str, user_id: str | None = None) -> dict[str, Any]:
    user_filter = "AND user_id = :user_id" if user_id is not None else ""
    row = await database.database.fetch_one(
        f"""
        SELECT id, user_id, title, object_type
        FROM knowledge_nodes
        WHERE id = :id AND object_type IN ('article', 'index') {user_filter}
        """,
        {"id": child_id, "user_id": user_id},
    )
    if not row:
        raise ValueError("child not found")
    return dict(row)


async def _would_create_cycle(index_id: str, child_id: str) -> bool:
    if index_id == child_id:
        return True
    row = await database.database.fetch_one(
        """
        WITH RECURSIVE descendants AS (
            SELECT child_id FROM index_children WHERE index_id = :child_id
          UNION
            SELECT ic.child_id FROM index_children ic
            JOIN descendants d ON d.child_id = ic.index_id
        )
        SELECT 1 AS found FROM descendants WHERE child_id = :index_id LIMIT 1
        """,
        {"index_id": index_id, "child_id": child_id},
    )
    return row is not None


async def add_child(
    index_id: str,
    child_id: str,
    user_id: str = "default",
    position: int | None = None,
    child_role: str = "member",
) -> dict[str, Any]:
    await _assert_index(index_id, user_id)
    await _assert_child(child_id, user_id)
    if await _would_create_cycle(index_id, child_id):
        raise ValueError("index cycle is not allowed")

    if position is None:
        row = await database.database.fetch_one(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM index_children WHERE index_id = :index_id",
            {"index_id": index_id},
        )
        position = int(row["next_position"] if row else 0)

    await database.database.execute(
        """
        INSERT INTO index_children (index_id, child_id, position, child_role)
        VALUES (:index_id, :child_id, :position, :child_role)
        ON CONFLICT (index_id, child_id) DO UPDATE SET
          position = EXCLUDED.position,
          child_role = EXCLUDED.child_role,
          updated_at = NOW()
        """,
        {"index_id": index_id, "child_id": child_id, "position": position, "child_role": child_role or "member"},
    )
    await mark_index_stale(index_id)
    return {"index_id": index_id, "child_id": child_id, "position": position, "child_role": child_role or "member"}


async def remove_child(index_id: str, child_id: str, user_id: str = "default") -> int:
    await _assert_index(index_id, user_id)
    result = await database.database.execute(
        "DELETE FROM index_children WHERE index_id = :index_id AND child_id = :child_id",
        {"index_id": index_id, "child_id": child_id},
    )
    await mark_index_stale(index_id)
    return int(result.split()[-1]) if isinstance(result, str) and result.split() else 0


async def reorder_children(index_id: str, child_ids: list[str], user_id: str = "default") -> None:
    await _assert_index(index_id, user_id)
    if not child_ids:
        return
    existing = await database.database.fetch_all(
        "SELECT child_id FROM index_children WHERE index_id = :index_id", {"index_id": index_id},
    )
    existing_ids = {r["child_id"] for r in existing}
    missing = [cid for cid in child_ids if cid not in existing_ids]
    if missing:
        raise ValueError(f"children not in index: {', '.join(missing)}")
    for pos, child_id in enumerate(child_ids):
        await database.database.execute(
            "UPDATE index_children SET position = :position, updated_at = NOW() WHERE index_id = :index_id AND child_id = :child_id",
            {"index_id": index_id, "child_id": child_id, "position": pos},
        )
    await mark_index_stale(index_id)


async def get_children(index_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        SELECT ic.index_id, ic.child_id, ic.position, ic.child_role,
               kn.title, kn.object_type, kn.abstract, kn.created_at
        FROM index_children ic
        JOIN knowledge_nodes kn ON kn.id = ic.child_id
        WHERE ic.index_id = :index_id AND kn.user_id = :user_id
        ORDER BY ic.position ASC, ic.created_at ASC
        """,
        {"index_id": index_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


async def get_parents(object_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        SELECT ic.index_id, ic.child_id, ic.position, ic.child_role,
               kn.title, kn.object_type, kn.abstract, kn.created_at
        FROM index_children ic
        JOIN knowledge_nodes kn ON kn.id = ic.index_id
        WHERE ic.child_id = :object_id AND kn.user_id = :user_id
        ORDER BY kn.title NULLS LAST, ic.created_at ASC
        """,
        {"object_id": object_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


async def get_ancestors(object_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        WITH RECURSIVE ancestors AS (
            SELECT ic.index_id, ic.child_id, 1 AS depth
            FROM index_children ic WHERE ic.child_id = :object_id
          UNION
            SELECT ic.index_id, ic.child_id, a.depth + 1
            FROM index_children ic JOIN ancestors a ON a.index_id = ic.child_id
            WHERE a.depth < 20
        )
        SELECT a.index_id, a.child_id, a.depth, kn.title, kn.object_type, kn.abstract
        FROM ancestors a JOIN knowledge_nodes kn ON kn.id = a.index_id
        WHERE kn.user_id = :user_id
        ORDER BY a.depth ASC, kn.title NULLS LAST
        """,
        {"object_id": object_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


async def get_descendants(index_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        WITH RECURSIVE descendants AS (
            SELECT ic.index_id, ic.child_id, ic.position, ic.child_role, 1 AS depth
            FROM index_children ic WHERE ic.index_id = :index_id
          UNION
            SELECT ic.index_id, ic.child_id, ic.position, ic.child_role, d.depth + 1
            FROM index_children ic JOIN descendants d ON d.child_id = ic.index_id
            WHERE d.depth < 20
        )
        SELECT d.index_id, d.child_id, d.position, d.child_role, d.depth,
               kn.title, kn.object_type, kn.abstract
        FROM descendants d JOIN knowledge_nodes kn ON kn.id = d.child_id
        WHERE kn.user_id = :user_id
        ORDER BY d.depth ASC, d.position ASC
        """,
        {"index_id": index_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


# ── Entity facts and signals ───────────────────────────────────────────────────

async def upsert_entity_fact(
    entity_id: str,
    article_id: str,
    fact_text: str,
    *,
    user_id: str = "default",
    evidence_span: str | None = None,
    confidence: float = 0.5,
) -> bool:
    fact_text = (fact_text or "").strip()
    if not fact_text:
        return False

    article = await database.database.fetch_one(
        """
        SELECT n.id, n.user_id, n.title, n.published_at, n.ingested_at,
               an.source_item_id AS article_source_item_id,
               an.source_published_at AS article_source_published_at,
               an.effective_at AS article_effective_at,
               an.captured_at AS article_captured_at
        FROM knowledge_nodes n
        LEFT JOIN article_nodes an ON an.node_id = n.id
        WHERE n.id = :article_id AND n.object_type = 'article'
        """,
        {"article_id": article_id},
    )
    if not article:
        return False

    source_item_id = article["article_source_item_id"]
    source_published_at = article["article_source_published_at"]
    fact_time = (
        article["article_effective_at"]
        or article["article_source_published_at"]
        or article["article_captured_at"]
        or article["published_at"]
        or article["ingested_at"]
    )

    existing = await database.database.fetch_one(
        "SELECT id FROM entity_facts WHERE entity_id = :entity_id AND article_id = :article_id AND fact_text = :fact_text",
        {"entity_id": entity_id, "article_id": article_id, "fact_text": fact_text},
    )

    try:
        await database.database.execute(
            """
            INSERT INTO entity_facts
              (user_id, entity_id, article_id, source_item_id, fact_text, fact_time,
               source_published_at, evidence_span, confidence)
            VALUES
              (:user_id, :entity_id, :article_id, :source_item_id, :fact_text,
               :fact_time, :source_published_at, :evidence_span, :confidence)
            ON CONFLICT (entity_id, article_id, fact_text) DO UPDATE SET
              source_item_id = EXCLUDED.source_item_id,
              fact_time = EXCLUDED.fact_time,
              source_published_at = EXCLUDED.source_published_at,
              evidence_span = COALESCE(EXCLUDED.evidence_span, entity_facts.evidence_span),
              confidence = GREATEST(entity_facts.confidence, EXCLUDED.confidence),
              updated_at = NOW()
            """,
            {
                "user_id": user_id or article["user_id"] or "default",
                "entity_id": entity_id,
                "article_id": article_id,
                "source_item_id": source_item_id,
                "fact_text": fact_text,
                "fact_time": fact_time,
                "source_published_at": source_published_at,
                "evidence_span": evidence_span,
                "confidence": max(0.0, min(float(confidence), 1.0)),
            },
        )
    except Exception as e:
        if "ForeignKeyViolationError" in type(e).__name__ or "foreign key" in str(e).lower():
            return False
        raise
    return existing is None


async def upsert_fact_from_mention(
    entity_id: str,
    article_id: str,
    *,
    canonical_name: str | None = None,
    summary_hint: str | None = None,
    salience: float = 0.5,
    user_id: str = "default",
) -> bool:
    if not canonical_name:
        entity = await database.database.fetch_one(
            """
            SELECT COALESCE(en.canonical_name, n.title) AS name
            FROM knowledge_nodes n
            LEFT JOIN entity_nodes en ON en.node_id = n.id
            WHERE n.id = :entity_id
            """,
            {"entity_id": entity_id},
        )
        canonical_name = entity["name"] if entity else entity_id

    article = await database.database.fetch_one(
        "SELECT title, abstract FROM knowledge_nodes WHERE id = :article_id",
        {"article_id": article_id},
    )
    article_title = article["title"] if article else article_id
    hint = (summary_hint or "").strip()
    if hint:
        fact_text = hint[:1000]
        evidence_span = hint[:300]
    else:
        fact_text = f"{canonical_name} is mentioned in {article_title or article_id}."
        evidence_span = canonical_name
    return await upsert_entity_fact(
        entity_id, article_id, fact_text,
        user_id=user_id, evidence_span=evidence_span, confidence=salience,
    )


async def backfill_entity_facts_from_mentions(user_id: str = "default") -> dict[str, int]:
    rows = await database.database.fetch_all(
        """
        SELECT ke.from_node_id AS article_id, ke.to_node_id AS entity_id,
               ke.weight, COALESCE(en.canonical_name, n.title) AS canonical_name
        FROM knowledge_edges ke
        JOIN knowledge_nodes article ON article.id = ke.from_node_id
        JOIN knowledge_nodes n ON n.id = ke.to_node_id
        LEFT JOIN entity_nodes en ON en.node_id = n.id
        WHERE article.user_id = :user_id
          AND article.object_type = 'article'
          AND n.object_type = 'entity'
          AND ke.relation_type = 'mentions'
        """,
        {"user_id": user_id},
    )
    inserted = 0
    for row in rows:
        created = await upsert_fact_from_mention(
            row["entity_id"], row["article_id"],
            canonical_name=row["canonical_name"],
            salience=float(row["weight"] or 0.5),
            user_id=user_id,
        )
        if created:
            inserted += 1
    return {"mentions_checked": len(rows), "facts_inserted": inserted}


async def refresh_entity_profile(entity_id: str) -> dict[str, Any]:
    """Regenerate entity abstract from recent facts (deterministic, no LLM call)."""
    entity = await database.database.fetch_one(
        """
        SELECT n.id, COALESCE(en.canonical_name, n.title) AS canonical_name
        FROM knowledge_nodes n
        LEFT JOIN entity_nodes en ON en.node_id = n.id
        WHERE n.id = :entity_id AND n.object_type = 'entity'
        """,
        {"entity_id": entity_id},
    )
    if not entity:
        return {"entity_id": entity_id, "refreshed": False, "reason": "not_found"}

    facts = await database.database.fetch_all(
        """
        SELECT fact_text, fact_time FROM entity_facts
        WHERE entity_id = :entity_id
        ORDER BY fact_time DESC NULLS LAST, updated_at DESC
        LIMIT :facts_limit
        """,
        {"entity_id": entity_id, "facts_limit": settings.entity_insights.refresh_facts_limit},
    )
    facts_count_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS count FROM entity_facts WHERE entity_id = :entity_id",
        {"entity_id": entity_id},
    )
    facts_count = int(facts_count_row["count"] if facts_count_row else 0)
    name = entity["canonical_name"] or entity_id
    if facts:
        new_abstract = f"{name} appears in {facts_count} source-grounded facts. " + " ".join(
            f["fact_text"] for f in facts[:3]
        )
    else:
        new_abstract = f"{name} has no extracted source-grounded facts yet."

    await database.database.execute(
        "UPDATE knowledge_nodes SET abstract = :abstract, updated_at = NOW() WHERE id = :entity_id",
        {"abstract": new_abstract, "entity_id": entity_id},
    )
    return {"entity_id": entity_id, "refreshed": True, "facts_count": facts_count}


async def lm_refresh_entity_abstract(entity_id: str) -> dict[str, Any]:
    """用 LLM 更新 entity abstract，同时重算 embedding。abstract_stale 置 false。"""
    from kb.retrieval import claude_client, embed_text
    from kb.common import vector_literal

    entity = await database.database.fetch_one(
        """
        SELECT n.id, COALESCE(en.canonical_name, n.title) AS canonical_name,
               en.aliases, n.abstract
        FROM knowledge_nodes n
        JOIN entity_nodes en ON en.node_id = n.id
        WHERE n.id = :id AND n.object_type = 'entity'
        """,
        {"id": entity_id},
    )
    if not entity:
        return {"entity_id": entity_id, "refreshed": False, "reason": "not_found"}

    articles = await database.database.fetch_all(
        """
        SELECT n.title, n.abstract
        FROM knowledge_edges ke
        JOIN knowledge_nodes n ON n.id = ke.from_node_id
        WHERE ke.to_node_id = :entity_id
          AND ke.relation_type = 'mentions'
          AND n.object_type = 'article'
          AND n.abstract IS NOT NULL AND n.abstract != ''
        ORDER BY n.published_at DESC NULLS LAST
        LIMIT :limit
        """,
        {"entity_id": entity_id, "limit": settings.ingestion.max_entity_page_sources},
    )
    if not articles:
        await database.database.execute(
            "UPDATE entity_nodes SET abstract_stale = false, updated_at = NOW() WHERE node_id = :id",
            {"id": entity_id},
        )
        return {"entity_id": entity_id, "refreshed": False, "reason": "no_mentions"}

    source_abstracts = "\n\n".join(
        f"《{a['title'] or entity_id}》: {a['abstract']}" for a in articles
    )
    existing_body = entity["abstract"] or ""

    message = await claude_client.messages.create(
        model=settings.models.entity_update,
        max_tokens=settings.llm_output_tokens.entity_update,
        messages=[{"role": "user", "content": prompts.entity_update(
            entity_name=entity["canonical_name"],
            existing_body=existing_body,
            new_source_abstracts=source_abstracts,
        )}],
    )
    new_abstract = getattr(message.content[0], "text", "").strip()
    if not new_abstract:
        return {"entity_id": entity_id, "refreshed": False, "reason": "empty_response"}

    new_embedding = await embed_text(new_abstract)
    embedding_literal = vector_literal(new_embedding)

    await database.database.execute(
        f"""
        UPDATE knowledge_nodes
        SET abstract = :abstract,
            embedding = '{embedding_literal}'::vector,
            embedding_model = :model,
            updated_at = NOW()
        WHERE id = :id
        """,
        {"abstract": new_abstract, "model": settings.embedding.model, "id": entity_id},
    )
    await database.database.execute(
        "UPDATE entity_nodes SET abstract_stale = false, updated_at = NOW() WHERE node_id = :id",
        {"id": entity_id},
    )
    return {"entity_id": entity_id, "refreshed": True}


async def refresh_stale_entity_abstracts(
    user_id: str = "default",
    batch_size: int | None = None,
) -> dict[str, int]:
    """批量 LLM 刷新 abstract_stale=true 的 entity，每次处理 entity_update_batch 个。"""
    limit = batch_size or settings.maintenance.entity_update_batch
    rows = await database.database.fetch_all(
        """
        SELECT en.node_id
        FROM entity_nodes en
        JOIN knowledge_nodes n ON n.id = en.node_id
        WHERE n.user_id = :user_id
          AND en.abstract_stale = true
          AND en.merged_into IS NULL
        ORDER BY n.updated_at ASC
        LIMIT :limit
        """,
        {"user_id": user_id, "limit": limit},
    )
    refreshed = 0
    failed = 0
    for row in rows:
        try:
            result = await lm_refresh_entity_abstract(row["node_id"])
            if result.get("refreshed"):
                refreshed += 1
        except Exception as exc:
            logger.warning("[entity-refresh] %s failed: %s", row["node_id"], exc)
            failed += 1
    return {"stale_found": len(rows), "refreshed": refreshed, "failed": failed}


async def rebuild_entity_pair_signals(user_id: str = "default") -> dict[str, int]:
    """Rebuild co-occurrence signals between all entity pairs from mention edges."""
    rows = await database.database.fetch_all(
        """
        SELECT ke.from_node_id AS article_id, ke.to_node_id AS entity_id
        FROM knowledge_edges ke
        JOIN knowledge_nodes article ON article.id = ke.from_node_id
        JOIN knowledge_nodes entity ON entity.id = ke.to_node_id
        WHERE article.user_id = :user_id
          AND article.object_type = 'article'
          AND entity.object_type = 'entity'
          AND ke.relation_type = 'mentions'
        """,
        {"user_id": user_id},
    )

    article_entities: dict[str, set[str]] = {}
    for row in rows:
        article_entities.setdefault(row["article_id"], set()).add(row["entity_id"])

    pair_articles: dict[tuple[str, str], set[str]] = {}
    for article_id, entity_ids in article_entities.items():
        ordered = sorted(entity_ids)
        for i, entity_a in enumerate(ordered):
            for entity_b in ordered[i + 1:]:
                pair_articles.setdefault(_ordered_pair(entity_a, entity_b), set()).add(article_id)

    await database.database.execute("DELETE FROM entity_pair_signals")

    if not pair_articles:
        return {"pairs_rebuilt": 0, "mentions_checked": len(rows)}

    max_count = max(len(articles) for articles in pair_articles.values())
    inserted = 0
    for (entity_a, entity_b), articles in pair_articles.items():
        count = len(articles)
        co_score = math.log(1 + count) / math.log(1 + max_count) if max_count > 0 else 0.0
        relatedness_score = max(0.0, min(co_score * 0.85, 1.0))
        article_ids = sorted(articles)
        await database.database.execute(
            """
            INSERT INTO entity_pair_signals
              (entity_a_id, entity_b_id, co_occurrence_count,
               co_occurrence_score, embedding_similarity, graph_proximity_score,
               temporal_score, relatedness_score, explanation, source_article_ids, updated_at)
            VALUES
              (:entity_a_id, :entity_b_id, :co_occurrence_count,
               :co_occurrence_score, 0, 0, 0,
               :relatedness_score, :explanation, :source_article_ids, NOW())
            ON CONFLICT (entity_a_id, entity_b_id) DO UPDATE SET
              co_occurrence_count = EXCLUDED.co_occurrence_count,
              co_occurrence_score = EXCLUDED.co_occurrence_score,
              embedding_similarity = EXCLUDED.embedding_similarity,
              graph_proximity_score = EXCLUDED.graph_proximity_score,
              temporal_score = EXCLUDED.temporal_score,
              relatedness_score = EXCLUDED.relatedness_score,
              explanation = EXCLUDED.explanation,
              source_article_ids = EXCLUDED.source_article_ids,
              updated_at = NOW()
            """,
            {
                "entity_a_id": entity_a,
                "entity_b_id": entity_b,
                "co_occurrence_count": count,
                "co_occurrence_score": co_score,
                "relatedness_score": relatedness_score,
                "explanation": f"共同出现于 {count} 篇 article",
                "source_article_ids": article_ids,
            },
        )
        inserted += 1

    return {"pairs_rebuilt": inserted, "mentions_checked": len(rows)}

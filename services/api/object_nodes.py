from typing import Any

import database


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
        for key in (
            "source_item_id",
            "raw_ref",
            "source_type",
            "source_published_at",
            "source_updated_at",
            "captured_at",
            "effective_at",
            "tags",
        ):
            if extra.get(key) is not None:
                node[key] = extra[key]
        node["article_status"] = extra.get("status")
    elif object_type == "summary":
        node["summary_of"] = extra.get("summary_of") or node.get("summary_of")
        node["perspective_label"] = extra.get("perspective_label") or node.get("perspective_label")
        node["perspective_instruction"] = extra.get("perspective_instruction") or node.get("perspective_instruction")
        node["is_default"] = extra.get("is_default") if extra.get("is_default") is not None else node.get("is_default")
        if extra.get("body") is not None:
            node["abstract"] = extra["body"]
        node["summary_source"] = extra.get("source")
    elif object_type == "entity":
        node["canonical_name"] = extra.get("canonical_name") or node.get("canonical_name")
        node["aliases"] = extra.get("aliases") if extra.get("aliases") is not None else node.get("aliases")
        node["entity_type"] = extra.get("entity_type")
        node["merged_into"] = extra.get("merged_into")
    elif object_type == "index":
        node["description"] = extra.get("description")
        node["rollup_instruction"] = extra.get("rollup_instruction")
        node["abstract_stale"] = extra.get("abstract_stale")

    return node

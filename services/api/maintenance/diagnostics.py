import database
from settings import settings

LEGACY_LLM_EDGE_TYPES = ("extends", "background_of", "supports", "contradicts")


async def cleanup_legacy_llm_edges() -> dict:
    """Delete legacy LLM semantic edges that are no longer part of the graph model."""
    rows = await database.database.fetch_all(
        """
        SELECT relation_type, COUNT(*) AS count
        FROM knowledge_edges
        WHERE relation_type = ANY(:types)
        GROUP BY relation_type
        """,
        {"types": list(LEGACY_LLM_EDGE_TYPES)},
    )
    before = {r["relation_type"]: r["count"] for r in rows}
    await database.database.execute(
        """
        DELETE FROM knowledge_edges
        WHERE relation_type = ANY(:types)
        """,
        {"types": list(LEGACY_LLM_EDGE_TYPES)},
    )
    return {"deleted": before, "total_deleted": sum(before.values())}


async def migrate_wikilink_edges() -> dict:
    """
    一次性迁移：将历史 wikilink 边改名为 mentions，并补填真实 salience 权重。
    幂等：若无 wikilink 边则跳过。
    """
    count_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM knowledge_edges WHERE relation_type = 'wikilink'"
    )
    n = count_row["n"] if count_row else 0
    if n == 0:
        return {"migrated": 0}

    # Step A: 用 entity_facts.confidence 更新权重（per-article salience 来源已迁移到 entity_facts）
    await database.database.execute(
        """
        UPDATE knowledge_edges ke
        SET weight = COALESCE(
            (SELECT ef.confidence
             FROM entity_facts ef
             WHERE ef.entity_id = ke.to_node_id
               AND ef.article_id = ke.from_node_id
             LIMIT 1),
            0.5
        )
        WHERE ke.relation_type = 'wikilink'
    """
    )

    # Step B: 重命名
    await database.database.execute(
        "UPDATE knowledge_edges SET relation_type = 'mentions' WHERE relation_type = 'wikilink'"
    )

    return {"migrated": n}


async def detect_embedding_model_drift(user_id: str = "default") -> dict:
    """
    检测 embedding_model 与当前 config 不匹配（或 NULL）的节点数量。
    仅做检测+报告，不自动重算——重算 embedding 是大动作，应由人工触发专门的
    re-embed 作业，避免与 maintenance 普通流程混合。

    返回：{current_model, mismatched_total, by_model, by_object_type, sample_ids}
    """
    current_model = settings.embedding.model

    summary_row = await database.database.fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM knowledge_nodes
        WHERE user_id = :uid
          AND embedding IS NOT NULL
          AND (embedding_model IS NULL OR embedding_model <> :model)
        """,
        {"uid": user_id, "model": current_model},
    )
    mismatched_total = int(summary_row["n"] or 0) if summary_row else 0

    by_model_rows = await database.database.fetch_all(
        """
        SELECT COALESCE(embedding_model, '(null)') AS model, COUNT(*) AS n
        FROM knowledge_nodes
        WHERE user_id = :uid
          AND embedding IS NOT NULL
          AND (embedding_model IS NULL OR embedding_model <> :model)
        GROUP BY COALESCE(embedding_model, '(null)')
        ORDER BY n DESC
        """,
        {"uid": user_id, "model": current_model},
    )
    by_object_rows = await database.database.fetch_all(
        """
        SELECT object_type, COUNT(*) AS n
        FROM knowledge_nodes
        WHERE user_id = :uid
          AND embedding IS NOT NULL
          AND (embedding_model IS NULL OR embedding_model <> :model)
        GROUP BY object_type
        ORDER BY n DESC
        """,
        {"uid": user_id, "model": current_model},
    )
    sample_rows = await database.database.fetch_all(
        """
        SELECT id FROM knowledge_nodes
        WHERE user_id = :uid
          AND embedding IS NOT NULL
          AND (embedding_model IS NULL OR embedding_model <> :model)
        ORDER BY created_at DESC
        LIMIT 20
        """,
        {"uid": user_id, "model": current_model},
    )

    return {
        "current_model": current_model,
        "mismatched_total": mismatched_total,
        "by_model": [{"model": r["model"], "count": int(r["n"])} for r in by_model_rows],
        "by_object_type": [
            {"object_type": r["object_type"], "count": int(r["n"])} for r in by_object_rows
        ],
        "sample_ids": [r["id"] for r in sample_rows],
    }

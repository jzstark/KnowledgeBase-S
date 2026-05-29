"""
每周知识库维护：
  1. 迁移历史 wikilink 边到 mentions
  2. entity candidate 晋升与 wikilink/mentions 回灌
  3. orphan entity 标记
  4. index abstract 聚合
  5. embedding_model drift 检测（仅报告，不自动重算）

可以作为独立脚本运行（python -m maintenance），也可以由 API 端点触发。
注：summarizes 关系由 summary_nodes.summary_of FK 表达，不再有 summarizes 边回填。
"""
import json

import database
from kb.graph import backfill_entity_facts_from_mentions, rebuild_entity_pair_signals

from .diagnostics import (
    cleanup_legacy_llm_edges,
    detect_embedding_model_drift,
    migrate_wikilink_edges,
)
from .entity_ops import (
    backfill_wikilinks_for_entity,
    cleanup_orphan_entities,
    promote_entity_candidates,
)
from .index_ops import aggregate_index_abstracts
from .restore import rebuild_from_raw, restore_from_wiki

USER_ID = "default"

__all__ = [
    "run_maintenance",
    "aggregate_index_abstracts",
    "rebuild_from_raw",
    "restore_from_wiki",
    "backfill_wikilinks_for_entity",
]


async def run_maintenance(user_id: str = USER_ID) -> dict:
    """
    运行全部维护任务。
    - 由 API 端点触发时：database 已由 main.py lifespan 连接，直接使用
    - 作为独立脚本运行时：__main__.py 负责调用 database.init()
    """
    print(f"[maintenance] Starting for user_id={user_id}", flush=True)

    legacy_cleanup_result = await cleanup_legacy_llm_edges()
    print(f"[maintenance] Legacy LLM edge cleanup: {legacy_cleanup_result}", flush=True)

    migrate_result = await migrate_wikilink_edges()
    print(f"[maintenance] Wikilink migration: {migrate_result}", flush=True)

    promote_result = await promote_entity_candidates(user_id)
    print(f"[maintenance] Entity promotion: {promote_result}", flush=True)

    # Backfill wikilinks for all entities into existing articles
    entity_rows = await database.database.fetch_all(
        "SELECT id FROM knowledge_nodes WHERE user_id = :uid AND object_type = 'entity'",
        {"uid": user_id},
    )
    wikilink_total = 0
    for ent in entity_rows:
        r = await backfill_wikilinks_for_entity(ent["id"], user_id)
        wikilink_total += r.get("wikilinks_added", 0)
    wikilink_result = {"entities_processed": len(entity_rows), "wikilinks_added": wikilink_total}
    print(f"[maintenance] Wikilink backfill: {wikilink_result}", flush=True)

    facts_result = await backfill_entity_facts_from_mentions(user_id)
    print(f"[maintenance] Entity facts backfill: {facts_result}", flush=True)

    # entity_profiles 表已删除；entity 描述统一回到 nodes.abstract（regenerate 端点按需更新）
    relatedness_result = await rebuild_entity_pair_signals(user_id)
    print(f"[maintenance] Entity relatedness refresh: {relatedness_result}", flush=True)

    orphan_result = await cleanup_orphan_entities(user_id)
    print(f"[maintenance] Orphan entities: {orphan_result}", flush=True)

    index_abstract_result = await aggregate_index_abstracts(user_id)
    print(f"[maintenance] Index abstract aggregation: {index_abstract_result}", flush=True)

    drift_result = await detect_embedding_model_drift(user_id)
    print(
        f"[maintenance] Embedding model drift: {drift_result['mismatched_total']} "
        f"nodes do not match current model '{drift_result['current_model']}'",
        flush=True,
    )

    summary = {
        "legacy_llm_edge_cleanup": legacy_cleanup_result,
        "wikilink_migration": migrate_result,
        "entity_promotion": promote_result,
        "wikilink_backfill": wikilink_result,
        "entity_facts": facts_result,
        "entity_relatedness": relatedness_result,
        "orphan_entities": orphan_result,
        "index_abstract": index_abstract_result,
        "embedding_model_drift": drift_result,
    }
    print(f"[maintenance] Done: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return summary

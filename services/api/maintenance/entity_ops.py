import os
import pathlib

import httpx

import database
from settings import settings


async def promote_entity_candidates(user_id: str) -> dict:
    """
    遍历未晋升的 entity_candidates，对满足晋升条件的条目生成 entity 页。
    补充摄入时未触发的晋升（维护兜底路径）。
    """
    api_base = os.environ.get("API_BASE_URL", "http://localhost:8000")

    rows = await database.database.fetch_all(
        """
        SELECT id, canonical_name, aliases, source_article_ids, mention_count, max_salience
        FROM entity_candidates
        WHERE user_id = :uid AND promoted_entity_id IS NULL
        ORDER BY mention_count DESC, max_salience DESC
        LIMIT 50
        """,
        {"uid": user_id},
    )

    promoted_count = 0
    for row in rows:
        row = dict(row)
        mention_count = int(row["mention_count"] or 0)
        max_salience = float(row["max_salience"] or 0)

        should_promote = (
            (max_salience >= settings.entity.promotion_salience
             and mention_count >= settings.entity.promotion_salience_mentions)
            or mention_count >= settings.entity.promotion_min_mentions
        )
        if not should_promote:
            continue

        source_ids = list(row["source_article_ids"] or [])
        aliases = list(row["aliases"]) if row["aliases"] else []

        # Ingest entity node
        try:
            async with httpx.AsyncClient() as http:
                ingest_resp = await http.post(
                    f"{api_base}/api/kb/ingest",
                    json={
                        "user_id": user_id,
                        "title": row["canonical_name"],
                        "abstract": row["canonical_name"],
                        "embedding": [],
                        "source_type": "entity",
                        "source_id": "maintenance",
                        "raw_ref": {},
                        "tags": [],
                        "object_type": "entity",
                        "source_node_ids": source_ids,
                        "canonical_name": row["canonical_name"],
                        "aliases": aliases,
                    },
                    timeout=30,
                )
                ingest_resp.raise_for_status()
                entity_node_id = ingest_resp.json().get("id")

            from kb.ingest import mark_candidate_promoted
            await mark_candidate_promoted(row["id"], {"entity_node_id": entity_node_id}, _={})
            promoted_count += 1
        except Exception as e:
            print(f"[maintenance] failed to ingest entity {row['canonical_name']}: {e}")

    return {"candidates_checked": len(rows), "promoted": promoted_count}


async def backfill_wikilinks_for_entity(entity_id: str, user_id: str) -> dict:
    """
    新 entity 晋升后，回扫所有 article 正文，在第一次出现 canonical_name / aliases 处
    注入 [[entity_id|原文]] wikilink，并更新 frontmatter + knowledge_edges。
    边使用 relation_type='mentions'，weight 取自 entity_candidates 中的真实 salience。
    """
    entity_row = await database.database.fetch_one(
        """
        SELECT en.canonical_name, en.aliases
        FROM knowledge_nodes n
        JOIN entity_nodes en ON en.node_id = n.id
        WHERE n.id = :id AND n.object_type = 'entity'
        """,
        {"id": entity_id},
    )
    if not entity_row:
        return {"articles_scanned": 0, "wikilinks_added": 0}

    canonical = entity_row["canonical_name"] or ""
    aliases = list(entity_row["aliases"] or [])
    search_terms = [t for t in ([canonical] + aliases) if t]
    if not search_terms:
        return {"articles_scanned": 0, "wikilinks_added": 0}

    # Pre-fetch salience map from entity_facts: {article_id: confidence}
    # （per-article salience 在 ingestion 时通过 upsert_fact_from_mention 已固化到 entity_facts.confidence；
    #  entity_candidates 已不再保存 per-article 详情，只保留聚合计数器与 source_article_ids 数组）
    salience_map: dict[str, float] = {}
    fact_rows = await database.database.fetch_all(
        "SELECT article_id, confidence FROM entity_facts WHERE entity_id = :eid",
        {"eid": entity_id},
    )
    for r in fact_rows:
        aid = r["article_id"]
        if aid:
            salience_map[aid] = float(r["confidence"] or 0.5)

    articles = await database.database.fetch_all(
        """SELECT n.id, n.user_id FROM entity_sources es
           JOIN knowledge_nodes n ON n.id = es.article_id
           WHERE es.entity_id = :eid AND n.user_id = :uid AND n.object_type = 'article'""",
        {"eid": entity_id, "uid": user_id},
    )

    user_data_dir = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
    wikilinks_added = 0

    for art in articles:
        art = dict(art)
        wiki_file = user_data_dir / art["user_id"] / "wiki" / "articles" / f"{art['id']}.md"
        if not wiki_file.exists():
            continue

        content = wiki_file.read_text(encoding="utf-8")
        modified = content

        for term in search_terms:
            # Skip if already wikilinked
            if f"[[{entity_id}" in modified or f"[[{term}]]" in modified:
                continue
            # Replace first occurrence (case-sensitive exact match)
            idx = modified.find(term)
            if idx == -1:
                continue
            # Don't link inside frontmatter (before second ---)
            fm_end = modified.find("---", 3)
            if fm_end != -1 and idx < fm_end + 3:
                continue
            replacement = f"[[{entity_id}|{term}]]"
            modified = modified[:idx] + replacement + modified[idx + len(term):]
            break

        if modified == content:
            continue

        wiki_file.write_text(modified, encoding="utf-8")

        # The source relation was recorded during ingestion; this only decorates its Wiki text.
        salience = salience_map.get(art["id"], 0.5)

        await database.database.execute(
            """
            INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
            VALUES (:from_id, :to_id, 'mentions', :weight, 'backfill')
            ON CONFLICT DO NOTHING
            """,
            {"from_id": art["id"], "to_id": entity_id, "weight": salience},
        )
        wikilinks_added += 1

    return {"articles_scanned": len(articles), "wikilinks_added": wikilinks_added}


async def cleanup_orphan_entities(user_id: str) -> dict:
    """找出没有来源文章的 entity 节点，标记为待审核（打 tag: orphan）。"""
    rows = await database.database.fetch_all(
        """
        SELECT n.id, n.title, n.tags
        FROM knowledge_nodes n
        WHERE n.user_id = :uid
          AND n.object_type = 'entity'
          AND NOT EXISTS (SELECT 1 FROM entity_sources es WHERE es.entity_id = n.id)
          AND NOT EXISTS (SELECT 1 FROM entity_facts ef WHERE ef.entity_id = n.id AND ef.article_id IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM knowledge_edges ke
                          WHERE ke.to_node_id = n.id AND ke.relation_type = 'mentions')
          AND NOT EXISTS (SELECT 1 FROM entity_candidates ec
                          WHERE ec.promoted_entity_id = n.id AND cardinality(ec.source_article_ids) > 0)
        """,
        {"uid": user_id},
    )
    marked = 0
    for row in rows:
        row = dict(row)
        tags = list(row["tags"] or [])
        if "orphan" not in tags:
            tags.append("orphan")
            await database.database.execute(
                "UPDATE knowledge_nodes SET tags = :tags, updated_at = NOW() WHERE id = :id",
                {"tags": tags, "id": row["id"]},
            )
            marked += 1
    return {"orphans_found": len(rows), "tagged": marked}

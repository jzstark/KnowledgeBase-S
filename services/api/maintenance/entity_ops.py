import os
import pathlib

import anthropic
import httpx

import database
from settings import settings
from prompts import prompts
from kb.common import message_text
from kb.graph import refresh_entity_profile, upsert_fact_from_mention


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
        # Fetch abstracts for source articles
        source_abstracts = []
        for art_id in source_ids[:settings.ingestion.max_entity_page_sources]:
            art = await database.database.fetch_one(
                "SELECT title, abstract FROM knowledge_nodes WHERE id = :id",
                {"id": art_id},
            )
            if art and art["abstract"]:
                source_abstracts.append(f"《{art['title'] or art_id}》: {art['abstract']}")

        aliases = list(row["aliases"]) if row["aliases"] else []

        # Call Claude to generate entity page
        try:
            prompt = prompts.entity_page(
                entity_name=row["canonical_name"],
                aliases="、".join(aliases) if aliases else "无",
                source_abstracts="\n\n".join(source_abstracts) or "（暂无来源信息）",
            )
            claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
            claude_client = anthropic.AsyncAnthropic(api_key=claude_api_key)
            resp = await claude_client.messages.create(
                model=settings.models.entity_page,
                max_tokens=settings.llm_output_tokens.entity_page,
                messages=[{"role": "user", "content": prompt}],
            )
            entity_body = message_text(resp)
        except Exception as e:
            print(f"[maintenance] entity page generation failed for {row['canonical_name']}: {e}")
            continue

        # Ingest entity node
        try:
            async with httpx.AsyncClient() as http:
                ingest_resp = await http.post(
                    f"{api_base}/api/kb/ingest",
                    json={
                        "user_id": user_id,
                        "title": row["canonical_name"],
                        "abstract": entity_body[:500],
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
                entity_node_id = ingest_resp.json().get("id")

            # wiki file written by write_wiki_node background task via ingest endpoint

            # Mark candidate as promoted
            await database.database.execute(
                "UPDATE entity_candidates SET promoted_entity_id = :eid WHERE id = :cid",
                {"eid": entity_node_id, "cid": row["id"]},
            )
            for article_id in source_ids:
                await upsert_fact_from_mention(
                    entity_node_id,
                    article_id,
                    canonical_name=row["canonical_name"],
                    salience=max_salience or 0.5,
                    user_id=user_id,
                )
            await refresh_entity_profile(entity_node_id)
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
        "SELECT id, user_id FROM knowledge_nodes WHERE user_id = :uid AND object_type = 'article'",
        {"uid": user_id},
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

        # Use real salience if available; default 0.5 for text-scan finds not in entity_candidates
        salience = salience_map.get(art["id"], 0.5)

        await database.database.execute(
            """
            INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
            VALUES (:from_id, :to_id, 'mentions', :weight, 'backfill')
            ON CONFLICT DO NOTHING
            """,
            {"from_id": art["id"], "to_id": entity_id, "weight": salience},
        )
        await upsert_fact_from_mention(
            entity_id,
            art["id"],
            canonical_name=canonical,
            salience=salience,
            user_id=user_id,
        )

        wikilinks_added += 1

    return {"articles_scanned": len(articles), "wikilinks_added": wikilinks_added}


async def cleanup_orphan_entities(user_id: str) -> dict:
    """找出没有 source-grounded facts 的 entity 节点，标记为待审核（打 tag: orphan）。"""
    rows = await database.database.fetch_all(
        """
        SELECT n.id, n.title, n.tags
        FROM knowledge_nodes n
        LEFT JOIN entity_facts ef ON ef.entity_id = n.id
        WHERE n.user_id = :uid
          AND n.object_type = 'entity'
        GROUP BY n.id
        HAVING COUNT(ef.id) = 0
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

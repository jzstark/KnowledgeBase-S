import math
from typing import Any

import database


def _ordered_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


async def mark_profile_stale(entity_id: str) -> None:
    await database.database.execute(
        """
        INSERT INTO entity_profiles (entity_id, status, updated_at)
        VALUES (:entity_id, 'stale', NOW())
        ON CONFLICT (entity_id) DO UPDATE SET
          status = 'stale',
          updated_at = NOW()
        """,
        {"entity_id": entity_id},
    )


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
        SELECT n.id, n.user_id, n.title, n.source_item_id, n.source_published_at,
               n.effective_at, n.captured_at, n.ingested_at,
               an.source_item_id AS article_source_item_id,
               an.source_published_at AS article_source_published_at,
               an.effective_at AS article_effective_at,
               an.captured_at AS article_captured_at
        FROM knowledge_nodes n
        LEFT JOIN article_nodes an ON an.node_id = n.id
        WHERE n.id = :article_id
          AND n.object_type = 'article'
        """,
        {"article_id": article_id},
    )
    if not article:
        return False

    source_item_id = article["article_source_item_id"] or article["source_item_id"]
    source_published_at = article["article_source_published_at"] or article["source_published_at"]
    fact_time = (
        article["article_effective_at"]
        or article["article_source_published_at"]
        or article["article_captured_at"]
        or article["effective_at"]
        or article["source_published_at"]
        or article["captured_at"]
        or article["ingested_at"]
    )

    existing = await database.database.fetch_one(
        """
        SELECT id FROM entity_facts
        WHERE entity_id = :entity_id
          AND article_id = :article_id
          AND fact_text = :fact_text
        """,
        {"entity_id": entity_id, "article_id": article_id, "fact_text": fact_text},
    )

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
    await mark_profile_stale(entity_id)
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
    entity = None
    if not canonical_name:
        entity = await database.database.fetch_one(
            """
            SELECT COALESCE(en.canonical_name, n.canonical_name, n.title) AS name
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
        entity_id,
        article_id,
        fact_text,
        user_id=user_id,
        evidence_span=evidence_span,
        confidence=salience,
    )


async def backfill_entity_facts_from_mentions(user_id: str = "default") -> dict[str, int]:
    rows = await database.database.fetch_all(
        """
        SELECT ke.from_node_id AS article_id, ke.to_node_id AS entity_id,
               ke.weight, COALESCE(en.canonical_name, n.canonical_name, n.title) AS canonical_name
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
            row["entity_id"],
            row["article_id"],
            canonical_name=row["canonical_name"],
            salience=float(row["weight"] or 0.5),
            user_id=user_id,
        )
        if created:
            inserted += 1
    return {"mentions_checked": len(rows), "facts_inserted": inserted}


async def refresh_entity_profile(entity_id: str) -> dict[str, Any]:
    entity = await database.database.fetch_one(
        """
        SELECT n.id, COALESCE(en.canonical_name, n.canonical_name, n.title) AS canonical_name
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
        SELECT fact_text, fact_time
        FROM entity_facts
        WHERE entity_id = :entity_id
        ORDER BY fact_time DESC NULLS LAST, updated_at DESC
        LIMIT 12
        """,
        {"entity_id": entity_id},
    )
    facts_count_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS count FROM entity_facts WHERE entity_id = :entity_id",
        {"entity_id": entity_id},
    )
    facts_count = int(facts_count_row["count"] if facts_count_row else 0)
    name = entity["canonical_name"] or entity_id
    if facts:
        profile_text = f"{name} appears in {facts_count} source-grounded facts. " + " ".join(
            f["fact_text"] for f in facts[:3]
        )
        timeline_items = []
        for f in reversed(facts[:6]):
            prefix = f["fact_time"].date().isoformat() if f["fact_time"] else "undated"
            timeline_items.append(f"{prefix}: {f['fact_text']}")
        timeline_summary = "\n".join(timeline_items)
    else:
        profile_text = f"{name} has no extracted source-grounded facts yet."
        timeline_summary = ""

    await database.database.execute(
        """
        INSERT INTO entity_profiles
          (entity_id, profile_text, timeline_summary, status, facts_count,
           refreshed_at, updated_at)
        VALUES
          (:entity_id, :profile_text, :timeline_summary, 'fresh', :facts_count,
           NOW(), NOW())
        ON CONFLICT (entity_id) DO UPDATE SET
          profile_text = EXCLUDED.profile_text,
          timeline_summary = EXCLUDED.timeline_summary,
          status = 'fresh',
          facts_count = EXCLUDED.facts_count,
          refreshed_at = NOW(),
          updated_at = NOW()
        """,
        {
            "entity_id": entity_id,
            "profile_text": profile_text,
            "timeline_summary": timeline_summary,
            "facts_count": facts_count,
        },
    )
    return {"entity_id": entity_id, "refreshed": True, "facts_count": facts_count}


async def refresh_stale_entity_profiles(user_id: str = "default", limit: int = 200) -> dict[str, int]:
    rows = await database.database.fetch_all(
        """
        SELECT n.id
        FROM knowledge_nodes n
        LEFT JOIN entity_profiles ep ON ep.entity_id = n.id
        WHERE n.user_id = :user_id
          AND n.object_type = 'entity'
          AND (ep.entity_id IS NULL OR ep.status = 'stale')
        ORDER BY n.updated_at DESC
        LIMIT :limit
        """,
        {"user_id": user_id, "limit": limit},
    )
    refreshed = 0
    for row in rows:
        result = await refresh_entity_profile(row["id"])
        if result.get("refreshed"):
            refreshed += 1
    return {"profiles_checked": len(rows), "profiles_refreshed": refreshed}


async def rebuild_entity_pair_signals(user_id: str = "default") -> dict[str, int]:
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
    min_count = 1
    min_score = 0.0
    inserted = 0
    for (entity_a, entity_b), articles in pair_articles.items():
        count = len(articles)
        co_score = math.log(1 + count) / math.log(1 + max_count) if max_count > 0 else 0.0
        temporal_score = 0.0
        relatedness_score = max(0.0, min(co_score * 0.85 + temporal_score * 0.15, 1.0))
        if count < min_count or relatedness_score < min_score:
            continue
        article_ids = sorted(articles)
        explanation = f"共同出现于 {count} 篇 article"
        await database.database.execute(
            """
            INSERT INTO entity_pair_signals
              (entity_a_id, entity_b_id, co_occurrence_count,
               co_occurrence_score, embedding_similarity, graph_proximity_score,
               temporal_score, relatedness_score, explanation, source_article_ids,
               updated_at)
            VALUES
              (:entity_a_id, :entity_b_id, :co_occurrence_count,
               :co_occurrence_score, 0, 0, :temporal_score,
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
                "temporal_score": temporal_score,
                "relatedness_score": relatedness_score,
                "explanation": explanation,
                "source_article_ids": article_ids,
            },
        )
        inserted += 1

    return {"pairs_rebuilt": inserted, "mentions_checked": len(rows)}

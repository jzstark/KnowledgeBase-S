import asyncio
import json
from datetime import datetime

import config_loader
import database
from kb.common import USER_ID
from kb.retrieval import embed_query


async def fetch_node_light(node_id: str) -> dict | None:
    row = await database.database.fetch_one(
        "SELECT id, title, abstract AS summary, tags FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    return dict(row) if row else None


async def fetch_briefing_source_articles(user_id: str, node_cutoff_sql: str) -> list[dict]:
    knowledge_time_sql = "COALESCE(n.effective_at, n.source_published_at, n.captured_at, n.ingested_at)"
    rows = await database.database.fetch_all(
        f"""
        SELECT n.id, n.title, n.abstract, n.tags, n.created_at,
               {knowledge_time_sql} AS knowledge_time
        FROM knowledge_nodes n
        JOIN sources s ON s.id = n.source_id
        WHERE n.user_id = :user_id
          AND s.is_primary = true
          AND n.object_type = 'article'
          AND {node_cutoff_sql}
        ORDER BY knowledge_time DESC
        """,
        {"user_id": user_id},
    )
    return [dict(r) for r in rows]


def _coerce_json_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _reference_url(raw_ref: dict, origin_ref: str | None) -> str:
    raw_url = raw_ref.get("url") or raw_ref.get("href")
    if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://")):
        return raw_url
    if origin_ref and origin_ref.startswith(("http://", "https://")):
        return origin_ref
    return ""


async def fetch_reference_sources(node_ids: list[str], user_id: str = USER_ID) -> list[dict]:
    seed_ids = list(dict.fromkeys(node_ids))
    if not seed_ids:
        return []

    rows = await database.database.fetch_all(
        """
        WITH seed_nodes AS (
            SELECT n.id, n.object_type, sn.summary_of, sn.source
            FROM knowledge_nodes n
            LEFT JOIN summary_nodes sn ON sn.node_id = n.id
            WHERE n.user_id = :user_id AND n.id = ANY(:seed_ids)
        ),
        expanded_ids AS (
            SELECT id FROM seed_nodes
            UNION
            SELECT summary_of FROM seed_nodes WHERE summary_of IS NOT NULL
            UNION
            SELECT jsonb_array_elements_text(
                COALESCE(COALESCE(source, '{}'::jsonb)->'source_node_ids', '[]'::jsonb)
            )
            FROM seed_nodes
            WHERE object_type = 'summary'
            UNION
            SELECT ef.article_id
            FROM entity_facts ef
            JOIN seed_nodes sn ON sn.id = ef.entity_id
            WHERE sn.object_type = 'entity' AND ef.article_id IS NOT NULL
        )
        SELECT n.id, n.title, n.object_type,
               COALESCE(an.source_type, n.object_type) AS source_type,
               an.raw_ref,
               si.origin_ref, si.source_published_at,
               s.name AS source_name
        FROM expanded_ids e
        JOIN knowledge_nodes n ON n.id = e.id
        LEFT JOIN article_nodes an ON an.node_id = n.id
        LEFT JOIN source_items si ON si.id = COALESCE(an.source_item_id, n.source_item_id)
        LEFT JOIN sources s ON s.id = COALESCE(si.source_id, n.source_id)
        WHERE n.user_id = :user_id
          AND n.object_type IN ('article', 'index')
        ORDER BY COALESCE(n.effective_at, n.source_published_at, si.source_published_at, n.created_at) DESC
        """,
        {"user_id": user_id, "seed_ids": seed_ids},
    )

    references = []
    seen_keys = set()
    for row in rows:
        item = dict(row)
        raw_ref = _coerce_json_dict(item.get("raw_ref"))
        url = _reference_url(raw_ref, item.get("origin_ref"))
        key = url or item["id"]
        if key in seen_keys:
            continue
        seen_keys.add(key)

        published_at = item.get("source_published_at")
        references.append(
            {
                "id": item["id"],
                "title": item.get("title") or item["id"],
                "url": url,
                "source_name": item.get("source_name"),
                "source_type": item.get("source_type") or item.get("object_type"),
                "published_at": published_at.isoformat() if published_at else "",
            }
        )
    return references


async def layered_retrieval(
    query: str,
    exclude_ids: list[str],
    user_id: str = USER_ID,
) -> dict[str, list[dict]]:
    summary_top_k = config_loader.get("retrieval.summary_top_k", 5)
    entity_top_k = config_loader.get("retrieval.entity_top_k", 10)
    article_direct_top_k = config_loader.get("retrieval.article_direct_top_k", 8)
    article_top_k = config_loader.get("retrieval.article_top_k", 8)
    entity_in_context = config_loader.get("retrieval.entity_in_context", 5)
    damping_e2s = config_loader.get("retrieval.damping_entity_to_summary", 0.7)
    damping_hop = config_loader.get("retrieval.damping_hop", 0.3)
    expansion_anchor_k = config_loader.get("retrieval.expansion_anchor_k", 5)
    expansion_min_score = config_loader.get("retrieval.expansion_min_score", 0.3)
    index_expand_thr = config_loader.get("retrieval.index_expand_threshold", 0.4)
    index_expand_limit = config_loader.get("retrieval.index_expand_limit", 3)
    fallback_discount = config_loader.get("retrieval.fallback_score_discount", 0.5)

    q_vec = await embed_query(query)
    emb_lit = "[" + ",".join(repr(x) for x in q_vec) + "]"
    excl_set = set(exclude_ids)
    excl_clause = (
        "AND kn.id NOT IN (" + ", ".join(f"'{i}'" for i in exclude_ids) + ")"
        if exclude_ids else ""
    )

    async def _vec_search(object_types: list[str], top_k: int) -> list[tuple[str, float]]:
        types_str = ", ".join(f"'{t}'" for t in object_types)
        if object_types == ["summary"]:
            score_sql = (
                "0.75 * (1-(COALESCE(sn.body_embedding, kn.embedding)<=>'"
                + emb_lit
                + "'::vector)) + 0.25 * (1-(COALESCE(sn.perspective_embedding, sn.body_embedding, kn.embedding)<=>'"
                + emb_lit
                + "'::vector))"
            )
            vector_filter = "(kn.embedding IS NOT NULL OR sn.body_embedding IS NOT NULL)"
            summary_join = "LEFT JOIN summary_nodes sn ON sn.node_id = kn.id"
        else:
            score_sql = f"1-(kn.embedding<=>'{emb_lit}'::vector)"
            vector_filter = "kn.embedding IS NOT NULL"
            summary_join = ""
        async with database.database.connection() as conn:
            rows = await conn.raw_connection.fetch(
                f"""
                SELECT kn.id, {score_sql} AS sim
                FROM knowledge_nodes kn
                {summary_join}
                WHERE kn.user_id = '{user_id}'
                  AND {vector_filter}
                  AND kn.object_type IN ({types_str})
                  {excl_clause}
                ORDER BY sim DESC
                LIMIT {top_k}
                """
            )
        return [(r["id"], float(r["sim"])) for r in rows]

    summary_hits, entity_hits, article_hits = await asyncio.gather(
        _vec_search(["summary"], summary_top_k),
        _vec_search(["entity"], entity_top_k),
        _vec_search(["article", "index"], article_direct_top_k),
    )

    entity_hits_map: dict[str, float] = {eid: s for eid, s in entity_hits}
    scored_summaries: dict[str, float] = {sid: s for sid, s in summary_hits}
    scored_articles: dict[str, float] = {}

    if entity_hits_map:
        ent_ids_str = ", ".join(f"'{e}'" for e in entity_hits_map)
        e2s = await database.database.fetch_all(
            f"""
            SELECT ke.from_node_id AS summary_id, ke.to_node_id AS entity_id, ke.weight
            FROM knowledge_edges ke
            JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
            WHERE ke.to_node_id IN ({ent_ids_str})
              AND ke.relation_type IN ('mentions', 'wikilink')
              AND kn.object_type = 'summary'
              AND kn.user_id = '{user_id}'
            """
        )
        for r in e2s:
            s_id = r["summary_id"]
            e_score = entity_hits_map.get(r["entity_id"], 0)
            prop = e_score * float(r["weight"] or 0.5) * damping_e2s
            if s_id not in scored_summaries or prop > scored_summaries[s_id]:
                scored_summaries[s_id] = prop

        e2a = await database.database.fetch_all(
            f"""
            SELECT ke.from_node_id AS article_id, ke.to_node_id AS entity_id, ke.weight
            FROM knowledge_edges ke
            JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
            WHERE ke.to_node_id IN ({ent_ids_str})
              AND ke.relation_type IN ('mentions', 'wikilink')
              AND kn.object_type IN ('article', 'index')
              AND kn.user_id = '{user_id}'
            """
        )
        for r in e2a:
            a_id = r["article_id"]
            e_score = entity_hits_map.get(r["entity_id"], 0)
            scored_articles[a_id] = scored_articles.get(a_id, 0) + e_score * float(r["weight"] or 0.5)

    if scored_summaries:
        sum_ids_str = ", ".join(f"'{s}'" for s in scored_summaries)
        s2a = await database.database.fetch_all(
            f"""
            SELECT node_id AS summary_id, summary_of AS target_id
            FROM summary_nodes
            WHERE node_id IN ({sum_ids_str})
              AND summary_of IS NOT NULL
            """
        )
        for r in s2a:
            t_id = r["target_id"]
            scored_articles[t_id] = scored_articles.get(t_id, 0) + scored_summaries[r["summary_id"]]

    anchors = [
        (a, s)
        for a, s in sorted(scored_articles.items(), key=lambda x: x[1], reverse=True)[:expansion_anchor_k]
        if s >= expansion_min_score
    ]
    if anchors:
        anchor_ids_str = ", ".join(f"'{a}'" for a, _ in anchors)
        anchor_score_map = dict(anchors)
        fwd_mentions = await database.database.fetch_all(
            f"""
            SELECT from_node_id AS article_id, to_node_id AS entity_id, weight
            FROM knowledge_edges
            WHERE from_node_id IN ({anchor_ids_str})
              AND relation_type IN ('mentions', 'wikilink')
            """
        )
        exp_entities: dict[str, float] = {}
        for r in fwd_mentions:
            combined = anchor_score_map[r["article_id"]] * float(r["weight"] or 0.5)
            if r["entity_id"] not in exp_entities or combined > exp_entities[r["entity_id"]]:
                exp_entities[r["entity_id"]] = combined

        if exp_entities:
            exp_ent_str = ", ".join(f"'{e}'" for e in exp_entities)
            rev_mentions = await database.database.fetch_all(
                f"""
                SELECT ke.from_node_id AS other_article, ke.to_node_id AS entity_id, ke.weight
                FROM knowledge_edges ke
                JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
                WHERE ke.to_node_id IN ({exp_ent_str})
                  AND ke.relation_type IN ('mentions', 'wikilink')
                  AND kn.object_type IN ('article', 'index')
                  AND kn.user_id = '{user_id}'
                """
            )
            for r in rev_mentions:
                o_id = r["other_article"]
                if o_id in scored_articles:
                    continue
                e_score = exp_entities.get(r["entity_id"], 0)
                scored_articles[o_id] = e_score * float(r["weight"] or 0.5) * damping_hop

    candidate_indices = [
        (nid, score)
        for nid, score in sorted(scored_articles.items(), key=lambda x: x[1], reverse=True)
        if score > index_expand_thr
    ]
    if candidate_indices:
        cand_ids_str = ", ".join(f"'{n}'" for n, _ in candidate_indices)
        idx_rows = await database.database.fetch_all(
            f"SELECT id FROM knowledge_nodes WHERE id IN ({cand_ids_str}) AND object_type = 'index'"
        )
        actual_indices = {r["id"] for r in idx_rows}
        high_indices = [(n, s) for n, s in candidate_indices if n in actual_indices][:index_expand_limit]

        for index_id, idx_score in high_indices:
            child_rows = await database.database.fetch_all(
                """
                SELECT ic.child_id
                FROM index_children ic
                JOIN knowledge_nodes kn ON kn.id = ic.child_id
                WHERE ic.index_id = :index_id
                  AND kn.object_type = 'article'
                  AND kn.user_id = :user_id
                ORDER BY ic.position ASC, ic.created_at ASC
                """,
                {"index_id": index_id, "user_id": user_id},
            )
            child_ids = [r["child_id"] for r in child_rows]
            if child_ids:
                child_ids_str = ", ".join(f"'{c}'" for c in child_ids)
                async with database.database.connection() as conn:
                    child_summary_sims = await conn.raw_connection.fetch(
                        f"""
                        SELECT sn.summary_of AS child_id,
                               0.75 * (1-(COALESCE(sn.body_embedding, kn.embedding)<=>'{emb_lit}'::vector))
                               + 0.25 * (1-(COALESCE(sn.perspective_embedding, sn.body_embedding, kn.embedding)<=>'{emb_lit}'::vector)) AS sim
                        FROM knowledge_nodes kn
                        JOIN summary_nodes sn ON sn.node_id = kn.id
                        WHERE sn.summary_of IN ({child_ids_str})
                          AND kn.object_type = 'summary'
                          AND (kn.embedding IS NOT NULL OR sn.body_embedding IS NOT NULL)
                        ORDER BY sim DESC
                        LIMIT {len(child_ids)}
                        """
                    )
                    child_sims = await conn.raw_connection.fetch(
                        f"""
                        SELECT id, 1-(embedding<=>'{emb_lit}'::vector) AS sim
                        FROM knowledge_nodes
                        WHERE id IN ({child_ids_str}) AND embedding IS NOT NULL
                        """
                    )
                for cs in child_summary_sims:
                    child_score = idx_score * float(cs["sim"])
                    scored_articles[cs["child_id"]] = max(scored_articles.get(cs["child_id"], 0), child_score)
                for cs in child_sims:
                    child_score = idx_score * float(cs["sim"])
                    scored_articles[cs["id"]] = max(scored_articles.get(cs["id"], 0), child_score)
            del scored_articles[index_id]

    non_excl_count = sum(1 for nid in scored_articles if nid not in excl_set)
    if non_excl_count < article_top_k:
        for node_id, sim in article_hits:
            if node_id not in scored_articles and node_id not in excl_set:
                scored_articles[node_id] = sim * fallback_discount

    final_ids = [
        nid
        for nid, _ in sorted(scored_articles.items(), key=lambda x: x[1], reverse=True)
        if nid not in excl_set
    ][:article_top_k]

    article_nodes: list[dict] = []
    if final_ids:
        ids_str = ", ".join(f"'{i}'" for i in final_ids)
        rows = await database.database.fetch_all(
            f"SELECT id, title, abstract, tags, object_type FROM knowledge_nodes WHERE id IN ({ids_str})"
        )
        node_map = {r["id"]: dict(r) for r in rows}
        for nid in final_ids:
            if nid in node_map:
                n = node_map[nid]
                n["summary"] = n.pop("abstract", "")
                n["score"] = scored_articles[nid]
                article_nodes.append(n)

    top_entity_ids = sorted(entity_hits_map, key=entity_hits_map.__getitem__, reverse=True)[:entity_in_context]
    entity_nodes: list[dict] = []
    if top_entity_ids:
        ids_str = ", ".join(f"'{i}'" for i in top_entity_ids)
        rows = await database.database.fetch_all(
            f"""
            SELECT n.id, n.title, en.canonical_name, n.abstract, n.tags
            FROM knowledge_nodes n
            LEFT JOIN entity_nodes en ON en.node_id = n.id
            WHERE n.id IN ({ids_str})
            """
        )
        node_map = {r["id"]: dict(r) for r in rows}
        for eid in top_entity_ids:
            if eid in node_map:
                n = node_map[eid]
                n["summary"] = n.pop("abstract", "")
                n["title"] = n["title"] or n.get("canonical_name") or ""
                entity_nodes.append(n)

    return {"articles": article_nodes, "entities": entity_nodes}

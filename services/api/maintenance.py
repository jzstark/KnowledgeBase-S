"""
每周知识库维护：
  1. fix_islands        — 孤岛检测：找无边节点，用 LLM 分析并建立语义边
  2. supplement_edges   — 补边：对仅有 similar_to 边的节点对，精化为更具体的关系类型
  3. detect_contradictions — 矛盾发现：检测相似节点对中存在的观点矛盾

可以作为独立脚本运行（python maintenance.py），也可以由 API 端点触发。
"""
import asyncio
import json
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
import config_loader
import database

USER_ID = "default"
CLAUDE_MODEL = config_loader.get("models.entity_page", "claude-haiku-4-5-20251001")


# ── LLM 关系分析 ──────────────────────────────────────────────────────────────

async def analyze_relation(
    id_a: str,
    abstract_a: str,
    id_b: str,
    abstract_b: str,
    client: anthropic.AsyncAnthropic,
) -> dict:
    """
    调用 Claude Haiku 判断两个知识节点之间的关系。
    返回：{"relation": str, "confidence": float, "from_id": str, "to_id": str}
    """
    prompt = (
        "以下是两个知识节点的摘要。请分析它们之间最有意义的关系。\n\n"
        f"节点 A：\n{abstract_a[:600]}\n\n"
        f"节点 B：\n{abstract_b[:600]}\n\n"
        "从以下关系类型中选择最合适的一种：\n"
        "- extends：一个节点是对另一个节点观点的延伸或深化\n"
        "- background_of：一个节点为理解另一个节点提供必要背景知识\n"
        "- contradicts：两个节点持明显相反的观点\n"
        "- supports：一个节点为另一个节点提供支持性证据或案例\n"
        "- none：没有明显的有意义关系\n\n"
        "以 JSON 格式输出（不含任何其他文字）：\n"
        '{"relation":"extends|background_of|contradicts|supports|none",'
        '"direction":"a_to_b|b_to_a|symmetric","confidence":0到1之间的小数}\n'
        "direction 说明：a_to_b 表示 A→B，b_to_a 表示 B→A，"
        "symmetric 表示双向（如 contradicts）。"
    )

    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text.strip())
    except Exception:
        return {"relation": "none", "confidence": 0.0, "from_id": id_a, "to_id": id_b}

    relation = result.get("relation", "none")
    direction = result.get("direction", "a_to_b")
    confidence = float(result.get("confidence", 0.0))

    if direction == "b_to_a":
        from_id, to_id = id_b, id_a
    else:
        from_id, to_id = id_a, id_b

    return {"relation": relation, "confidence": confidence, "from_id": from_id, "to_id": to_id}


async def upsert_llm_edge(from_id: str, to_id: str, relation: str, confidence: float) -> bool:
    """插入 LLM 推导的边，若相同三元组已存在则跳过。返回是否实际插入。"""
    existing = await database.database.fetch_one(
        """
        SELECT id FROM knowledge_edges
        WHERE from_node_id = :from_id AND to_node_id = :to_id AND relation_type = :rel
        """,
        {"from_id": from_id, "to_id": to_id, "rel": relation},
    )
    if existing:
        return False
    await database.database.execute(
        """
        INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
        VALUES (:from_id, :to_id, :rel, :weight, 'auto_llm')
        """,
        {"from_id": from_id, "to_id": to_id, "rel": relation, "weight": confidence},
    )
    return True


# ── 1. 孤岛检测 ───────────────────────────────────────────────────────────────

async def fix_islands(user_id: str, client: anthropic.AsyncAnthropic) -> dict:
    """找出无任何边的孤立节点，尝试用 LLM 为其建立语义边。"""
    islands = await database.database.fetch_all(
        """
        SELECT n.id, n.title, n.abstract
        FROM knowledge_nodes n
        WHERE n.user_id = :user_id
          AND n.embedding IS NOT NULL
          AND n.abstract IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM knowledge_edges e
            WHERE e.from_node_id = n.id OR e.to_node_id = n.id
          )
        LIMIT 20
        """,
        {"user_id": user_id},
    )
    if not islands:
        return {"islands_found": 0, "edges_added": 0}

    edges_added = 0
    for island in islands:
        island = dict(island)
        # 找 top-3 最相似的节点（asyncpg 原生接口支持向量运算符）
        async with database.database.connection() as conn:
            candidates = await conn.raw_connection.fetch(
                """
                SELECT id, title, abstract,
                       1 - (embedding <=> (
                         SELECT embedding FROM knowledge_nodes WHERE id = $1
                       )) AS sim
                FROM knowledge_nodes
                WHERE id != $1
                  AND user_id = $2
                  AND embedding IS NOT NULL
                  AND abstract IS NOT NULL
                ORDER BY embedding <=> (
                  SELECT embedding FROM knowledge_nodes WHERE id = $1
                )
                LIMIT 3
                """,
                island["id"], user_id,
            )

        for c in candidates:
            c = dict(c)
            if float(c["sim"]) < 0.55:
                continue
            result = await analyze_relation(
                island["id"], island["abstract"] or "",
                c["id"], c["abstract"] or "",
                client,
            )
            if result["relation"] != "none" and result["confidence"] >= 0.70:
                added = await upsert_llm_edge(
                    result["from_id"], result["to_id"],
                    result["relation"], result["confidence"],
                )
                if added:
                    edges_added += 1
                    break  # 每个孤岛建一条边即可打破孤立状态

    return {"islands_found": len(islands), "edges_added": edges_added}


# ── 2. 补边（将 similar_to 精化为更具体关系）────────────────────────────────────

async def supplement_edges(user_id: str, client: anthropic.AsyncAnthropic) -> dict:
    """
    对仅有 similar_to（auto_semantic）边的节点对，用 LLM 判断是否存在更精确的关系。
    每次最多处理 20 对，按相似度降序。
    """
    async with database.database.connection() as conn:
        pairs = await conn.raw_connection.fetch(
            """
            SELECT e.from_node_id, e.to_node_id, e.weight,
                   na.abstract AS abstract_a, nb.abstract AS abstract_b
            FROM knowledge_edges e
            JOIN knowledge_nodes na ON na.id = e.from_node_id
            JOIN knowledge_nodes nb ON nb.id = e.to_node_id
            WHERE e.relation_type = 'similar_to'
              AND e.created_by = 'auto_semantic'
              AND na.user_id = $1
              AND na.abstract IS NOT NULL
              AND nb.abstract IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_edges e2
                WHERE e2.created_by = 'auto_llm'
                  AND (
                    (e2.from_node_id = e.from_node_id AND e2.to_node_id = e.to_node_id)
                    OR (e2.from_node_id = e.to_node_id AND e2.to_node_id = e.from_node_id)
                  )
              )
            ORDER BY e.weight DESC
            LIMIT 20
            """,
            user_id,
        )

    if not pairs:
        return {"pairs_analyzed": 0, "edges_added": 0}

    edges_added = 0
    for p in pairs:
        p = dict(p)
        result = await analyze_relation(
            p["from_node_id"], p["abstract_a"],
            p["to_node_id"], p["abstract_b"],
            client,
        )
        if result["relation"] not in ("none", "similar_to") and result["confidence"] >= 0.70:
            added = await upsert_llm_edge(
                result["from_id"], result["to_id"],
                result["relation"], result["confidence"],
            )
            if added:
                edges_added += 1

    return {"pairs_analyzed": len(pairs), "edges_added": edges_added}


# ── 3. 矛盾发现 ───────────────────────────────────────────────────────────────

async def detect_contradictions(user_id: str, client: anthropic.AsyncAnthropic) -> dict:
    """
    对相似度适中（0.75~0.92）的 similar_to 节点对检测观点矛盾。
    相似度过高往往是同一事件不同报道，过低则主题不同，中间段最有可能出现"同题不同观点"。
    每次最多检查 10 对。
    """
    async with database.database.connection() as conn:
        pairs = await conn.raw_connection.fetch(
            """
            SELECT e.from_node_id, e.to_node_id, e.weight,
                   na.abstract AS abstract_a, nb.abstract AS abstract_b
            FROM knowledge_edges e
            JOIN knowledge_nodes na ON na.id = e.from_node_id
            JOIN knowledge_nodes nb ON nb.id = e.to_node_id
            WHERE e.relation_type = 'similar_to'
              AND e.created_by = 'auto_semantic'
              AND e.weight BETWEEN 0.75 AND 0.92
              AND na.user_id = $1
              AND na.abstract IS NOT NULL
              AND nb.abstract IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_edges e2
                WHERE e2.relation_type = 'contradicts'
                  AND (
                    (e2.from_node_id = e.from_node_id AND e2.to_node_id = e.to_node_id)
                    OR (e2.from_node_id = e.to_node_id AND e2.to_node_id = e.from_node_id)
                  )
              )
            ORDER BY e.weight DESC
            LIMIT 10
            """,
            user_id,
        )

    if not pairs:
        return {"pairs_checked": 0, "contradictions_found": 0}

    contradictions_found = 0
    for p in pairs:
        p = dict(p)
        result = await analyze_relation(
            p["from_node_id"], p["abstract_a"],
            p["to_node_id"], p["abstract_b"],
            client,
        )
        if result["relation"] == "contradicts" and result["confidence"] >= 0.75:
            added = await upsert_llm_edge(
                result["from_id"], result["to_id"],
                "contradicts", result["confidence"],
            )
            if added:
                contradictions_found += 1

    return {"pairs_checked": len(pairs), "contradictions_found": contradictions_found}


# ── 4. Entity 候选晋升扫描 ─────────────────────────────────────────────────────

async def promote_entity_candidates(user_id: str) -> dict:
    """
    遍历未晋升的 entity_candidates，对满足晋升条件的条目生成 entity 页。
    补充摄入时未触发的晋升（维护兜底路径）。
    """
    import httpx
    api_base = os.environ.get("API_BASE_URL", "http://localhost:8000")

    rows = await database.database.fetch_all(
        """
        SELECT id, canonical_name, aliases, mentions
        FROM entity_candidates
        WHERE user_id = :uid AND promoted_entity_id IS NULL
        ORDER BY jsonb_array_length(mentions) DESC
        LIMIT 50
        """,
        {"uid": user_id},
    )

    promoted_count = 0
    for row in rows:
        row = dict(row)
        mentions = row["mentions"]
        if isinstance(mentions, str):
            mentions = json.loads(mentions)
        mention_count = len(mentions)
        max_salience = max((m.get("salience", 0) for m in mentions), default=0)

        should_promote = (
            (max_salience >= config_loader.get("entity.promotion_salience", 0.7)
             and mention_count >= config_loader.get("entity.promotion_salience_mentions", 2))
            or mention_count >= config_loader.get("entity.promotion_min_mentions", 3)
        )
        if not should_promote:
            continue

        source_ids = [m["article_id"] for m in mentions]
        # Fetch abstracts for source articles
        source_abstracts = []
        for art_id in source_ids[:config_loader.get("ingestion.max_entity_page_sources", 5)]:
            art = await database.database.fetch_one(
                "SELECT title, abstract FROM knowledge_nodes WHERE id = :id",
                {"id": art_id},
            )
            if art and art["abstract"]:
                source_abstracts.append(f"《{art['title'] or art_id}》: {art['abstract']}")

        aliases = list(row["aliases"]) if row["aliases"] else []

        # Call Claude to generate entity page
        try:
            import prompt_loader
            prompt = prompt_loader.fill(
                "entity_page",
                entity_name=row["canonical_name"],
                aliases="、".join(aliases) if aliases else "无",
                source_abstracts="\n\n".join(source_abstracts) or "（暂无来源信息）",
            )
            claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
            claude_client = anthropic.AsyncAnthropic(api_key=claude_api_key)
            resp = await claude_client.messages.create(
                model=config_loader.get("models.entity_page", CLAUDE_MODEL),
                max_tokens=config_loader.get("llm_output_tokens.entity_page", 2048),
                messages=[{"role": "user", "content": prompt}],
            )
            entity_body = resp.content[0].text.strip()
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

            # Write entity wiki file
            wiki_dir = (
                sys.path[0] and None  # handled by write_wiki_node background task
            )

            # Mark candidate as promoted
            await database.database.execute(
                "UPDATE entity_candidates SET promoted_entity_id = :eid WHERE id = :cid",
                {"eid": entity_node_id, "cid": row["id"]},
            )
            promoted_count += 1
        except Exception as e:
            print(f"[maintenance] failed to ingest entity {row['canonical_name']}: {e}")

    return {"candidates_checked": len(rows), "promoted": promoted_count}


# ── 5. Wikilink 回灌 ───────────────────────────────────────────────────────────

async def backfill_wikilinks_for_entity(entity_id: str, user_id: str) -> dict:
    """
    新 entity 晋升后，回扫所有 article 正文，在第一次出现 canonical_name / aliases 处
    注入 [[entity_id|原文]] wikilink，并更新 frontmatter + knowledge_edges。
    边使用 relation_type='mentions'，weight 取自 entity_candidates 中的真实 salience。
    """
    import pathlib as _pathlib

    entity_row = await database.database.fetch_one(
        "SELECT canonical_name, aliases FROM knowledge_nodes WHERE id = :id",
        {"id": entity_id},
    )
    if not entity_row:
        return {"articles_scanned": 0, "wikilinks_added": 0}

    canonical = entity_row["canonical_name"] or ""
    aliases = list(entity_row["aliases"] or [])
    search_terms = [t for t in ([canonical] + aliases) if t]
    if not search_terms:
        return {"articles_scanned": 0, "wikilinks_added": 0}

    # Pre-fetch salience map from entity_candidates: {article_id: salience}
    salience_map: dict[str, float] = {}
    cand_row = await database.database.fetch_one(
        "SELECT mentions FROM entity_candidates WHERE promoted_entity_id = :eid",
        {"eid": entity_id},
    )
    if cand_row and cand_row["mentions"]:
        mentions_data = cand_row["mentions"]
        if isinstance(mentions_data, str):
            mentions_data = json.loads(mentions_data)
        for m in mentions_data:
            if m.get("article_id"):
                salience_map[m["article_id"]] = float(m.get("salience", 0.5))

    articles = await database.database.fetch_all(
        "SELECT id, user_id FROM knowledge_nodes WHERE user_id = :uid AND object_type = 'article'",
        {"uid": user_id},
    )

    user_data_dir = _pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
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
            """,
            {"from_id": art["id"], "to_id": entity_id, "weight": salience},
        )

        # Append article to entity's source_node_ids
        await database.database.execute(
            """
            UPDATE knowledge_nodes
            SET source_node_ids = array_append(COALESCE(source_node_ids, '{}'), :art_id),
                updated_at = NOW()
            WHERE id = :eid
              AND NOT (:art_id = ANY(COALESCE(source_node_ids, '{}')))
            """,
            {"eid": entity_id, "art_id": art["id"]},
        )
        wikilinks_added += 1

    return {"articles_scanned": len(articles), "wikilinks_added": wikilinks_added}


# ── 6. 历史 wikilink 边迁移 ──────────────────────────────────────────────────────

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

    # Step A: 用 entity_candidates 中的真实 salience 更新权重
    await database.database.execute(
        """
        UPDATE knowledge_edges ke
        SET weight = COALESCE(
            (SELECT (elem->>'salience')::float
             FROM entity_candidates ec,
                  jsonb_array_elements(ec.mentions) AS elem
             WHERE ec.promoted_entity_id = ke.to_node_id
               AND elem->>'article_id' = ke.from_node_id
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


# ── 7. 孤儿 Entity 清理 ────────────────────────────────────────────────────────

async def cleanup_orphan_entities(user_id: str) -> dict:
    """找出 source_node_ids 为空的 entity 节点，标记为待审核（打 tag: orphan）。"""
    rows = await database.database.fetch_all(
        """
        SELECT id, title, tags FROM knowledge_nodes
        WHERE user_id = :uid
          AND object_type = 'entity'
          AND (source_node_ids IS NULL OR source_node_ids = '{}')
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


# ── 8. Summarizes 边回填 ─────────────────────────────────────────────────────

async def backfill_summarizes_edges(user_id: str) -> dict:
    """为现有 summary 节点补建 summarizes 边（幂等，跳过已有的）。"""
    summaries = await database.database.fetch_all(
        """
        SELECT id, source_node_ids FROM knowledge_nodes
        WHERE user_id = :uid
          AND object_type = 'summary'
          AND source_node_ids IS NOT NULL
          AND source_node_ids != '{}'
        """,
        {"uid": user_id},
    )
    added = 0
    for row in summaries:
        row = dict(row)
        for target_id in (row["source_node_ids"] or []):
            exists = await database.database.fetch_one(
                """
                SELECT 1 FROM knowledge_edges
                WHERE from_node_id = :fid AND to_node_id = :tid AND relation_type = 'summarizes'
                """,
                {"fid": row["id"], "tid": target_id},
            )
            if not exists:
                await database.database.execute(
                    """
                    INSERT INTO knowledge_edges
                      (from_node_id, to_node_id, relation_type, weight, created_by)
                    VALUES (:from_id, :to_id, 'summarizes', 1.0, 'backfill')
                    """,
                    {"from_id": row["id"], "to_id": target_id},
                )
                added += 1
    return {"summaries_checked": len(summaries), "edges_added": added}


# ── 9. Index Abstract 聚合 ─────────────────────────────────────────────────────

async def aggregate_index_abstracts(user_id: str) -> dict:
    """
    为每个 index 节点生成聚合 abstract（底层向上）。

    收集直接子节点（via part_of 边）的 abstract，调用 LLM 生成 3-5 句综合摘要，
    更新 DB 中的 abstract 和 embedding，并刷新 wiki 文件 frontmatter。
    幂等：每次运行都用最新子节点状态覆盖。
    """
    import prompt_loader
    from openai import AsyncOpenAI
    from routers.kb import write_wiki_node

    claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    if not claude_api_key:
        return {"error": "CLAUDE_API_KEY not set", "processed": 0, "skipped": 0}

    claude_client = anthropic.AsyncAnthropic(api_key=claude_api_key)
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    max_children  = config_loader.get("ingestion.max_index_children_abstracts", 20)

    # 1. 找所有 index 节点
    index_rows = await database.database.fetch_all(
        "SELECT id, title FROM knowledge_nodes WHERE user_id = :uid AND object_type = 'index'",
        {"uid": user_id},
    )
    if not index_rows:
        return {"processed": 0, "skipped": 0}

    index_ids = {r["id"] for r in index_rows}

    # 2. 构建 child map: index_id → [(child_id, child_object_type)]
    child_map: dict[str, list[tuple[str, str]]] = {idx: [] for idx in index_ids}
    for idx_id in index_ids:
        rows = await database.database.fetch_all(
            f"""
            SELECT ke.from_node_id AS child_id, kn.object_type AS child_type
            FROM knowledge_edges ke
            JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
            WHERE ke.to_node_id = '{idx_id}'
              AND ke.relation_type = 'part_of'
              AND kn.user_id = '{user_id}'
            """
        )
        child_map[idx_id] = [(r["child_id"], r["child_type"]) for r in rows]

    # 3. 底层向上排序：无 index 子节点的先处理（满足 book→chapters 的典型两层结构）
    def has_index_children(idx_id: str) -> bool:
        return any(ctype == "index" for _, ctype in child_map.get(idx_id, []))

    ordered = sorted(index_rows, key=lambda r: (1 if has_index_children(r["id"]) else 0))

    processed = skipped = 0

    for idx_row in ordered:
        idx_id    = idx_row["id"]
        idx_title = idx_row["title"] or idx_id
        children  = child_map.get(idx_id, [])

        if not children:
            skipped += 1
            continue

        # 4. 收集子节点 abstract（从 DB 实时读取，确保 sub-index 已更新）
        child_abstracts: list[str] = []
        for child_id, _ in children[:max_children]:
            child = await database.database.fetch_one(
                "SELECT title, abstract FROM knowledge_nodes WHERE id = :id",
                {"id": child_id},
            )
            if child and child["abstract"]:
                label = child["title"] or child_id
                child_abstracts.append(f"- 《{label}》：{child['abstract']}")

        if not child_abstracts:
            skipped += 1
            continue

        # 5. 调用 LLM 生成聚合 abstract
        try:
            prompt = prompt_loader.fill(
                "index_summary",
                index_title=idx_title,
                child_abstracts="\n".join(child_abstracts),
            )
            resp = await claude_client.messages.create(
                model=config_loader.get("models.index_summary", "claude-haiku-4-5-20251001"),
                max_tokens=config_loader.get("llm_output_tokens.index_summary", 512),
                messages=[{"role": "user", "content": prompt}],
            )
            new_abstract = resp.content[0].text.strip()
        except Exception as e:
            print(f"[maintenance] index_abstract LLM error for {idx_id}: {e}", flush=True)
            skipped += 1
            continue

        # 6. 生成 embedding
        try:
            embed_resp = await openai_client.embeddings.create(
                model=config_loader.get("embedding.model", "text-embedding-3-small"),
                input=new_abstract[:config_loader.get("embedding.max_chars", 8000)],
                dimensions=config_loader.get("embedding.dimensions", 1536),
            )
            embedding  = embed_resp.data[0].embedding
            emb_lit    = "[" + ",".join(repr(x) for x in embedding) + "]"
        except Exception as e:
            print(f"[maintenance] index_abstract embed error for {idx_id}: {e}", flush=True)
            skipped += 1
            continue

        # 7. 更新 DB（abstract + embedding）
        await database.database.execute(
            f"""
            UPDATE knowledge_nodes
            SET abstract = :abstract, embedding = '{emb_lit}'::vector, updated_at = NOW()
            WHERE id = :id
            """,
            {"abstract": new_abstract, "id": idx_id},
        )

        # 8. 刷新 wiki 文件 frontmatter（write_wiki_node 保留已有 body；
        #    首次写入时以新 abstract 作为 body）
        await write_wiki_node(idx_id, user_id)

        processed += 1
        print(f"[maintenance] index_abstract updated: {idx_id} ({idx_title})", flush=True)

    return {"processed": processed, "skipped": skipped}


# ── 10. Restore From Wiki ────────────────────────────────────────────────────────

async def restore_from_wiki(user_id: str = USER_ID) -> dict:
    """
    从 wiki 文件重建 knowledge_nodes 和 knowledge_edges（用于 postgres 数据丢失时恢复）。

    流程：
      1. 扫描 wiki/{articles,summaries,entities,indices}/ 下所有 .md 文件
      2. 解析 frontmatter（id、type、title、tags、raw_ref 等）+ 提取 body 作为 abstract
      3. 用 OpenAI 生成 embedding
      4. INSERT 到 knowledge_nodes（跳过已存在的）
      5. 重建 edges：summarizes（来自 summary_of 字段）、part_of（来自 relations 字段）

    幂等：已存在的节点跳过，ON CONFLICT DO NOTHING 保护边。
    """
    import pathlib as _pl
    import yaml as _yaml
    from datetime import datetime as _dt
    from openai import AsyncOpenAI

    openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    user_data_dir = _pl.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
    wiki_dir = user_data_dir / user_id / "wiki"

    if not wiki_dir.exists():
        return {"error": "wiki directory not found", "nodes_inserted": 0}

    def _parse(path: _pl.Path) -> dict | None:
        import re as _re
        try:
            text = path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) < 3:
                return None
            fm_raw = parts[1]
            body = parts[2].strip()
            # Sanitize curly/fancy quotes in frontmatter values before yaml parse
            fm_safe = _re.sub(r'["“”]', '"', fm_raw)
            try:
                meta = _yaml.safe_load(fm_safe) or {}
            except Exception:
                # fallback: extract id and title with regex
                meta = {}
                for key in ("id", "type", "source_type", "raw_ref", "summary_of", "canonical_name"):
                    m = _re.search(rf'^{key}:\s*(.+)$', fm_raw, _re.MULTILINE)
                    if m:
                        meta[key] = m.group(1).strip().strip('"')
                title_m = _re.search(r'^title:\s*"?(.*?)"?\s*$', fm_raw, _re.MULTILINE)
                if title_m:
                    meta["title"] = title_m.group(1).strip('“”"')
            for marker in ["\n## 関連節点\n", "\n## 关联节点\n"]:
                if marker in body:
                    body = body[:body.index(marker)].strip()
            # strip leading "# Title\n\n"
            lines = body.split("\n", 2)
            if lines and lines[0].startswith("# "):
                body = lines[2].strip() if len(lines) >= 3 else ""
            meta["_body"] = body
            return meta
        except Exception as e:
            print(f"[restore] parse error {path.name}: {e}", flush=True)
            return None

    # ── 1. Collect ────────────────────────────────────────────────────────────
    all_metas: list[dict] = []
    for subdir in ["articles", "summaries", "entities", "indices"]:
        subpath = wiki_dir / subdir
        if subpath.exists():
            for f in sorted(subpath.glob("*.md")):
                m = _parse(f)
                if m and m.get("id"):
                    all_metas.append(m)

    print(f"[restore] found {len(all_metas)} wiki files", flush=True)

    # ── 2. Ensure placeholder sources exist ───────────────────────────────────
    VALID_TYPES = {"rss", "url", "plaintext", "pdf", "epub", "word", "image", "wechat"}
    seen_types: set[str] = set()
    for m in all_metas:
        st = (m.get("source_type") or "plaintext").lower()
        seen_types.add(st)
    for st in seen_types:
        src_id = f"restored_{st}"
        exists = await database.database.fetch_one(
            "SELECT id FROM sources WHERE id = :id", {"id": src_id}
        )
        if not exists:
            db_type = st if st in VALID_TYPES else "plaintext"
            await database.database.execute(
                """
                INSERT INTO sources (id, user_id, name, type, fetch_mode, is_primary, config)
                VALUES (:id, :uid, :name, :type, 'manual', true, '{}')
                """,
                {"id": src_id, "uid": user_id,
                 "name": f"[已恢复：{st}]", "type": db_type},
            )
            print(f"[restore] created placeholder source: {src_id}", flush=True)

    # ── 3. Insert nodes ───────────────────────────────────────────────────────
    nodes_inserted = nodes_skipped = 0

    for m in all_metas:
        node_id = m["id"]
        existing = await database.database.fetch_one(
            "SELECT id FROM knowledge_nodes WHERE id = :id", {"id": node_id}
        )
        if existing:
            nodes_skipped += 1
            continue

        object_type = str(m.get("type") or "article")
        body = m.get("_body") or ""

        if object_type == "summary":
            abstract = body
        else:
            abstract = body[:500] if body else (str(m.get("canonical_name") or m.get("title") or ""))

        # embedding
        try:
            embed_text = (abstract or str(m.get("title") or node_id))
            resp = await openai_client.embeddings.create(
                model=config_loader.get("embedding.model", "text-embedding-3-small"),
                input=embed_text[:config_loader.get("embedding.max_chars", 8000)],
                dimensions=config_loader.get("embedding.dimensions", 1536),
            )
            emb = resp.data[0].embedding
            emb_lit = "[" + ",".join(repr(x) for x in emb) + "]"
        except Exception as e:
            print(f"[restore] embed failed {node_id}: {e}", flush=True)
            dim = config_loader.get("embedding.dimensions", 1536)
            emb_lit = "[" + ",".join(["0.0"] * dim) + "]"

        # tags
        tags_raw = m.get("tags") or []
        if isinstance(tags_raw, str):
            import re as _re
            tags_raw = _re.findall(r'"([^"]*)"', tags_raw) or [t.strip() for t in tags_raw.split(",")]
        tags = [str(t).strip() for t in tags_raw if t]

        # source
        source_type = (m.get("source_type") or "plaintext").lower()
        source_id = f"restored_{source_type}"

        # raw_ref
        raw_ref_str = str(m.get("raw_ref") or "")
        if raw_ref_str.startswith("http"):
            raw_ref_dict: dict = {"type": "url", "url": raw_ref_str}
        elif "::chapter::" in raw_ref_str:
            raw_ref_dict = {"type": "book_chapter", "path": raw_ref_str}
        elif raw_ref_str:
            raw_ref_dict = {"type": "file", "path": raw_ref_str}
        else:
            raw_ref_dict = {}

        # dates
        created_at = m.get("created_at") or m.get("updated_at")
        if isinstance(created_at, str):
            try:
                created_at = _dt.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_at = None

        # entity fields
        canonical_name = m.get("canonical_name") or None
        aliases_raw = m.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases_raw = [a.strip().strip('"') for a in aliases_raw.strip("[]").split(",") if a.strip()]
        aliases = [str(a) for a in aliases_raw]

        # summary fields
        summary_of = m.get("summary_of") or None
        sources_raw = m.get("sources") or []
        if isinstance(sources_raw, str):
            sources_raw = [s.strip() for s in sources_raw.strip("[]").split(",") if s.strip()]
        source_node_ids = [str(s) for s in sources_raw]

        perspective = m.get("perspective") or None

        try:
            await database.database.execute(
                f"""
                INSERT INTO knowledge_nodes
                  (id, user_id, title, abstract, embedding, source_type, source_id,
                   raw_ref, tags, is_primary, object_type, source_node_ids,
                   summary_of, canonical_name, aliases, perspective, created_at)
                VALUES
                  (:id, :uid, :title, :abstract, '{emb_lit}'::vector,
                   :source_type, :source_id, :raw_ref, :tags, true,
                   :object_type, :source_node_ids, :summary_of,
                   :canonical_name, :aliases, :perspective, :created_at)
                """,
                {
                    "id": node_id, "uid": user_id,
                    "title": str(m.get("title") or node_id),
                    "abstract": abstract,
                    "source_type": source_type,
                    "source_id": source_id,
                    "raw_ref": database.jsonb(raw_ref_dict),
                    "tags": tags,
                    "object_type": object_type,
                    "source_node_ids": source_node_ids,
                    "summary_of": summary_of,
                    "canonical_name": canonical_name,
                    "aliases": aliases,
                    "perspective": perspective,
                    "created_at": created_at,
                },
            )
            nodes_inserted += 1
            print(f"[restore] {object_type}: {node_id} — {m.get('title', '')}", flush=True)
        except Exception as e:
            print(f"[restore] insert error {node_id}: {e}", flush=True)
            nodes_skipped += 1

    # ── 4. Reconstruct edges ──────────────────────────────────────────────────
    import re as _re
    edges_inserted = 0

    async def _add_edge(from_id: str, to_id: str, rel: str, weight: float = 1.0):
        nonlocal edges_inserted
        try:
            await database.database.execute(
                """
                INSERT INTO knowledge_edges
                  (from_node_id, to_node_id, relation_type, weight, created_by)
                VALUES (:f, :t, :r, :w, 'restore_from_wiki')
                ON CONFLICT DO NOTHING
                """,
                {"f": from_id, "t": to_id, "r": rel, "w": weight},
            )
            edges_inserted += 1
        except Exception as e:
            print(f"[restore] edge error {from_id}→{to_id}: {e}", flush=True)

    # Collect all known node IDs for validation
    known_ids: set[str] = {m["id"] for m in all_metas if m.get("id")}

    for m in all_metas:
        node_id = m["id"]
        object_type = str(m.get("type") or "article")

        # summarizes: summary → article
        if object_type == "summary" and m.get("summary_of"):
            await _add_edge(node_id, m["summary_of"], "summarizes")

        # part_of: article → index (from relations frontmatter added by write_wiki_node)
        relations = m.get("relations") or []
        if isinstance(relations, list):
            for rel in relations:
                if isinstance(rel, dict) and rel.get("type") == "part_of" and rel.get("id"):
                    await _add_edge(node_id, rel["id"], "part_of")

        # mentions: article/summary → entity  (scan [[entity_id|...]] in body)
        if object_type in ("article", "summary"):
            body = m.get("_body") or ""
            for target_id in set(_re.findall(r'\[\[((?:ent|nod)[_a-z0-9A-Z]+)(?:\|[^\]]+)?\]\]', body)):
                if target_id in known_ids:
                    await _add_edge(node_id, target_id, "mentions", 0.5)

    print(f"[restore] done: {nodes_inserted} nodes, {edges_inserted} edges", flush=True)
    return {
        "nodes_inserted": nodes_inserted,
        "nodes_skipped": nodes_skipped,
        "edges_inserted": edges_inserted,
    }


# ── 11. Rebuild From Raw ─────────────────────────────────────────────────────────

async def rebuild_from_raw(user_id: str = USER_ID) -> dict:
    """
    从 raw 文件重建知识库（幂等）。
    执行流程：
      1. 清空所有 file-sourced article/entity/summary 节点及 entity_candidates
      2. 删除对应 wiki 文件
      3. 重置 file-type source 的 last_fetched_at，触发 ingestion-worker 重新处理
      4. 轮询等待所有 source 完成（最长 60 分钟）
      5. 运行 run_maintenance()

    须在 api 容器中执行：
      docker compose exec api python maintenance.py rebuild_from_raw --confirm
    """
    import pathlib as _pathlib
    import httpx

    user_data_dir = _pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
    wiki_dir = user_data_dir / user_id / "wiki"
    ingestion_url = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")

    # ── Step 1: 清空可重建内容 ────────────────────────────────────────────────
    print("[rebuild] Step 1: 清空可重建内容...", flush=True)

    # 先删 entity_candidates（有 FK 约束指向 entity 节点，须先删）
    ec_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM entity_candidates WHERE user_id = :uid", {"uid": user_id}
    )
    ec_count = int(ec_row["n"]) if ec_row else 0
    await database.database.execute(
        "DELETE FROM entity_candidates WHERE user_id = :uid", {"uid": user_id}
    )

    # 再删 entity 节点（knowledge_edges ON DELETE CASCADE 自动清理边）
    ent_rows = await database.database.fetch_all(
        "SELECT id FROM knowledge_nodes WHERE user_id = :uid AND object_type = 'entity'",
        {"uid": user_id},
    )
    entity_ids = {r["id"] for r in ent_rows}
    await database.database.execute(
        "DELETE FROM knowledge_nodes WHERE user_id = :uid AND object_type = 'entity'",
        {"uid": user_id},
    )

    # 删 file-sourced 节点（summary_of FK ON DELETE CASCADE 自动级联删子 summary）
    art_rows = await database.database.fetch_all(
        """SELECT id FROM knowledge_nodes WHERE user_id = :uid
           AND source_type IN ('pdf', 'plaintext', 'word', 'image', 'wechat')""",
        {"uid": user_id},
    )
    article_ids = {r["id"] for r in art_rows}
    await database.database.execute(
        """DELETE FROM knowledge_nodes WHERE user_id = :uid
           AND source_type IN ('pdf', 'plaintext', 'word', 'image', 'wechat')""",
        {"uid": user_id},
    )

    print(
        f"[rebuild] 已清空: entity_candidates={ec_count}, "
        f"entities={len(entity_ids)}, file-sourced nodes={len(article_ids)} (含 cascade summary)",
        flush=True,
    )

    # ── Step 2: 清理 wiki 文件 ────────────────────────────────────────────────
    wiki_deleted = 0
    deleted_ids = entity_ids | article_ids

    # 按 ID 删除 article/entity/index 的 wiki 文件
    for subdir in ("articles", "entities", "indices"):
        d = wiki_dir / subdir
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and f.suffix == ".md" and f.stem in deleted_ids:
                f.unlink()
                wiki_deleted += 1

    # 重建删除的 summary wiki 文件：删除 wiki/summaries/ 下所有文件（全部重建）
    # 包括 auto-generated summaries（cascade 已删 DB 记录）和孤立文件
    sum_dir = wiki_dir / "summaries"
    if sum_dir.exists():
        for f in sum_dir.iterdir():
            if f.is_file() and f.suffix == ".md":
                f.unlink()
                wiki_deleted += 1

    print(f"[rebuild] 已删除 wiki 文件: {wiki_deleted} 个", flush=True)

    # ── Step 3: 重置 last_fetched_at，触发 ingestion-worker ──────────────────
    print("[rebuild] Step 3: 触发 ingestion-worker...", flush=True)

    sources = await database.database.fetch_all(
        """SELECT id, name FROM sources WHERE user_id = :uid
           AND type IN ('pdf', 'plaintext', 'word', 'image', 'wechat')""",
        {"uid": user_id},
    )
    for src in sources:
        await database.database.execute(
            "UPDATE sources SET last_fetched_at = NULL WHERE id = :id", {"id": src["id"]}
        )

    triggered: list[str] = []
    failed: list[str] = []
    async with httpx.AsyncClient() as http:
        for src in sources:
            try:
                resp = await http.post(f"{ingestion_url}/trigger/{src['id']}", timeout=10)
                data = resp.json()
                if resp.status_code == 200 and data.get("ok"):
                    triggered.append(src["id"])
                    print(f"[rebuild]   触发成功: {src['name']} ({src['id']})", flush=True)
                else:
                    failed.append(src["id"])
                    print(f"[rebuild]   触发失败: {src['name']} — {data.get('detail', resp.text)}", flush=True)
            except Exception as e:
                failed.append(src["id"])
                print(f"[rebuild]   触发异常: {src['name']} — {e}", flush=True)

    if not triggered:
        print(
            "[rebuild] 警告：未能触发任何 source（ingestion-worker 是否在运行？），跳过等待步骤。\n"
            "[rebuild] 可待 ingestion-worker 启动后手动触发，或重新执行 rebuild_from_raw。",
            flush=True,
        )
    else:
        # ── Step 4: 轮询等待所有 source 完成 ──────────────────────────────────
        print(
            f"[rebuild] Step 4: 等待 {len(triggered)} 个 source 完成（最长 60 分钟）...",
            flush=True,
        )
        max_wait = 3600
        interval = 20
        elapsed = 0
        pending = list(triggered)

        while elapsed < max_wait and pending:
            await asyncio.sleep(interval)
            elapsed += interval
            still = []
            for sid in pending:
                row = await database.database.fetch_one(
                    "SELECT last_fetched_at FROM sources WHERE id = :id", {"id": sid}
                )
                if row and row["last_fetched_at"] is None:
                    still.append(sid)
            if len(still) < len(pending):
                done = len(triggered) - len(still)
                print(
                    f"[rebuild]   进度: {done}/{len(triggered)} 完成 ({elapsed}s 已过)",
                    flush=True,
                )
            pending = still

        if pending:
            print(f"[rebuild] 警告：超时，以下 source 未完成: {pending}", flush=True)
        else:
            print("[rebuild] 所有 source 已完成 ingestion", flush=True)

    # ── Step 5: 运行维护任务 ──────────────────────────────────────────────────
    print("[rebuild] Step 5: 运行维护任务（entity 晋升、wikilink 回灌等）...", flush=True)
    maintenance_result = await run_maintenance(user_id)

    result = {
        "entity_candidates_deleted": ec_count,
        "entities_deleted": len(entity_ids),
        "file_nodes_deleted": len(article_ids),
        "wiki_files_deleted": wiki_deleted,
        "sources_triggered": len(triggered),
        "sources_failed": len(failed),
        "maintenance": maintenance_result,
    }
    print(f"[rebuild] 完成: {json.dumps(result, ensure_ascii=False)}", flush=True)
    return result


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def run_maintenance(user_id: str = USER_ID) -> dict:
    """
    运行全部维护任务。
    - 由 API 端点触发时：database 已由 main.py lifespan 连接，直接使用
    - 作为独立脚本运行时：__main__ 块负责调用 database.init()
    """
    claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not claude_api_key:
        print("[maintenance] ERROR: CLAUDE_API_KEY not set", flush=True)
        return {"error": "CLAUDE_API_KEY not set"}

    client = anthropic.AsyncAnthropic(api_key=claude_api_key)

    print(f"[maintenance] Starting for user_id={user_id}", flush=True)

    migrate_result = await migrate_wikilink_edges()
    print(f"[maintenance] Wikilink migration: {migrate_result}", flush=True)

    island_result = await fix_islands(user_id, client)
    print(f"[maintenance] Islands: {island_result}", flush=True)

    supplement_result = await supplement_edges(user_id, client)
    print(f"[maintenance] Supplement: {supplement_result}", flush=True)

    contradiction_result = await detect_contradictions(user_id, client)
    print(f"[maintenance] Contradictions: {contradiction_result}", flush=True)

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

    orphan_result = await cleanup_orphan_entities(user_id)
    print(f"[maintenance] Orphan entities: {orphan_result}", flush=True)

    summarizes_result = await backfill_summarizes_edges(user_id)
    print(f"[maintenance] Summarizes backfill: {summarizes_result}", flush=True)

    index_abstract_result = await aggregate_index_abstracts(user_id)
    print(f"[maintenance] Index abstract aggregation: {index_abstract_result}", flush=True)

    summary = {
        "wikilink_migration": migrate_result,
        "islands": island_result,
        "supplement": supplement_result,
        "contradictions": contradiction_result,
        "entity_promotion": promote_result,
        "wikilink_backfill": wikilink_result,
        "orphan_entities": orphan_result,
        "summarizes_backfill": summarizes_result,
        "index_abstract": index_abstract_result,
    }
    print(f"[maintenance] Done: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return summary


if __name__ == "__main__":
    async def main():
        await database.init()
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd == "restore_from_wiki":
            result = await restore_from_wiki()
        elif cmd == "rebuild_from_raw":
            if "--confirm" not in sys.argv:
                print(
                    "此操作将清空数据库中所有 file-sourced 节点（articles/entities/summaries）"
                    " 并通过 ingestion-worker 重新入库。\n"
                    "确认执行请加 --confirm 参数：\n"
                    "  python maintenance.py rebuild_from_raw --confirm\n"
                    "或通过 docker compose exec：\n"
                    "  docker compose exec api python maintenance.py rebuild_from_raw --confirm"
                )
                return
            result = await rebuild_from_raw()
        else:
            result = await run_maintenance()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(main())

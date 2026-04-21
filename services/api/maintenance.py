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
import database

USER_ID = "default"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


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
            (max_salience >= 0.7 and mention_count >= 2)
            or mention_count >= 3
        )
        if not should_promote:
            continue

        source_ids = [m["article_id"] for m in mentions]
        # Fetch abstracts for source articles
        source_abstracts = []
        for art_id in source_ids[:5]:
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
                model=CLAUDE_MODEL,
                max_tokens=2048,
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

        # Insert wikilink edge (ignore if exists)
        await database.database.execute(
            """
            INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
            VALUES (:from_id, :to_id, 'wikilink', 1.0, 'backfill')
            ON CONFLICT DO NOTHING
            """,
            {"from_id": art["id"], "to_id": entity_id},
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


# ── 6. 孤儿 Entity 清理 ────────────────────────────────────────────────────────

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

    summary = {
        "islands": island_result,
        "supplement": supplement_result,
        "contradictions": contradiction_result,
        "entity_promotion": promote_result,
        "wikilink_backfill": wikilink_result,
        "orphan_entities": orphan_result,
    }
    print(f"[maintenance] Done: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return summary


if __name__ == "__main__":
    async def main():
        await database.init()
        result = await run_maintenance()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(main())

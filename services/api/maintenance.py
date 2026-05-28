"""
每周知识库维护：
  1. 迁移历史 wikilink 边到 mentions
  2. entity candidate 晋升与 wikilink/mentions 回灌
  3. orphan entity 标记
  4. index abstract 聚合
  5. embedding_model drift 检测（仅报告，不自动重算）

可以作为独立脚本运行（python maintenance.py），也可以由 API 端点触发。
注：summarizes 关系由 summary_nodes.summary_of FK 表达，不再有 summarizes 边回填。
"""
import asyncio
import json
import os
import sys
from typing import Any

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
import config_loader
import database
import entity_insights
import index_structure
import object_nodes

USER_ID = "default"
CLAUDE_MODEL = config_loader.get("models.entity_page", "claude-haiku-4-5-20251001")
LEGACY_LLM_EDGE_TYPES = ("extends", "background_of", "supports", "contradicts")


# ── 1. Legacy LLM semantic edge cleanup ──────────────────────────────────────

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
            (max_salience >= config_loader.get("entity.promotion_salience", 0.7)
             and mention_count >= config_loader.get("entity.promotion_salience_mentions", 2))
            or mention_count >= config_loader.get("entity.promotion_min_mentions", 3)
        )
        if not should_promote:
            continue

        source_ids = list(row["source_article_ids"] or [])
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
            entity_body = getattr(resp.content[0], "text", "").strip()
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
                await entity_insights.upsert_fact_from_mention(
                    entity_node_id,
                    article_id,
                    canonical_name=row["canonical_name"],
                    salience=max_salience or 0.5,
                    user_id=user_id,
                )
            await entity_insights.refresh_entity_profile(entity_node_id)
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
            ON CONFLICT DO NOTHING
            """,
            {"from_id": art["id"], "to_id": entity_id, "weight": salience},
        )
        await entity_insights.upsert_fact_from_mention(
            entity_id,
            art["id"],
            canonical_name=canonical,
            salience=salience,
            user_id=user_id,
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


# ── 7. 孤儿 Entity 清理 ────────────────────────────────────────────────────────

async def detect_embedding_model_drift(user_id: str = USER_ID) -> dict:
    """
    检测 embedding_model 与当前 config 不匹配（或 NULL）的节点数量。
    仅做检测+报告，不自动重算——重算 embedding 是大动作，应由人工触发专门的
    re-embed 作业，避免与 maintenance 普通流程混合。

    返回：{current_model, mismatched_total, by_model, by_object_type, sample_ids}
    """
    current_model = config_loader.get("embedding.model", "text-embedding-3-small")

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


# ── 9. Index Abstract 聚合 ─────────────────────────────────────────────────────

async def aggregate_index_abstracts(
    user_id: str,
    index_id: str | None = None,
    only_stale: bool = False,
) -> dict:
    """
    为每个 index 节点生成聚合 abstract（底层向上）。

    收集直接子节点（via index_children）的 abstract，调用 LLM 生成 3-5 句综合摘要，
    更新 DB 中的 abstract 和 embedding，并刷新 wiki 文件 frontmatter。
    幂等：每次运行都用最新子节点状态覆盖。
    """
    import prompt_loader
    from openai import AsyncOpenAI
    from kb.wiki import write_wiki_node

    claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    if not claude_api_key:
        return {"error": "CLAUDE_API_KEY not set", "processed": 0, "skipped": 0}

    claude_client = anthropic.AsyncAnthropic(api_key=claude_api_key)
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    max_children  = config_loader.get("ingestion.max_index_children_abstracts", 20)

    # 1. 找所有 index 节点
    filters = ["kn.user_id = :uid", "kn.object_type = 'index'"]
    params: dict = {"uid": user_id}
    if index_id:
        filters.append("kn.id = :index_id")
        params["index_id"] = index_id
    if only_stale:
        filters.append("COALESCE(ix.abstract_stale, false) = true")
    index_rows = await database.database.fetch_all(
        f"""
        SELECT kn.id, kn.title, ix.rollup_instruction
        FROM knowledge_nodes kn
        LEFT JOIN index_nodes ix ON ix.node_id = kn.id
        WHERE {' AND '.join(filters)}
        """,
        params,
    )
    if not index_rows:
        return {"processed": 0, "skipped": 0}

    index_ids = {r["id"] for r in index_rows}

    # 2. 构建 child map: index_id → [(child_id, child_object_type)]
    child_map: dict[str, list[tuple[str, str]]] = {idx: [] for idx in index_ids}
    for idx_id in index_ids:
        rows = await database.database.fetch_all(
            """
            SELECT ic.child_id, kn.object_type AS child_type
            FROM index_children ic
            JOIN knowledge_nodes kn ON kn.id = ic.child_id
            WHERE ic.index_id = :idx_id
              AND kn.user_id = :user_id
            ORDER BY ic.position ASC, ic.created_at ASC
            """,
            {"idx_id": idx_id, "user_id": user_id},
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
        rollup_instruction = idx_row["rollup_instruction"] or ""
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
                child_abstracts=(
                    f"Rollup instruction: {rollup_instruction}\n\n" if rollup_instruction else ""
                ) + "\n".join(child_abstracts),
            )
            resp = await claude_client.messages.create(
                model=config_loader.get("models.index_summary", "claude-haiku-4-5-20251001"),
                max_tokens=config_loader.get("llm_output_tokens.index_summary", 512),
                messages=[{"role": "user", "content": prompt}],
            )
            new_abstract = getattr(resp.content[0], "text", "").strip()
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
            SET abstract = :abstract,
                embedding = '{emb_lit}'::vector,
                embedding_model = :embedding_model,
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "abstract": new_abstract,
                "id": idx_id,
                "embedding_model": config_loader.get("embedding.model", "text-embedding-3-small"),
            },
        )
        await object_nodes.upsert_object_node(
            idx_id,
            "index",
            {"abstract_stale": False},
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
    从 wiki 文件重建 knowledge_nodes / index_children / knowledge_edges（用于 postgres 数据丢失时恢复）。

    流程：
      1. 扫描 wiki/{articles,summaries,entities,indices}/ 下所有 .md 文件
      2. 解析 frontmatter（id、type、title、tags、raw_ref 等）+ 提取 body 作为 abstract
      3. 用 OpenAI 生成 embedding
      4. INSERT 到 knowledge_nodes（跳过已存在的）
      5. 重建 summarizes / mentions 边，并把 legacy part_of relations 迁移为 index_children

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
        published_at = None
        for time_key in ("published_at", "effective_at", "source_published_at", "captured_at"):
            value = m.get(time_key)
            if isinstance(value, str) and value:
                try:
                    published_at = _dt.fromisoformat(value.replace("Z", "+00:00"))
                    break
                except Exception:
                    continue
        published_at = published_at or created_at

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
                  (id, user_id, title, abstract, embedding, source_id,
                   tags, object_type, published_at, created_at, doc_kind, embedding_model)
                VALUES
                  (:id, :uid, :title, :abstract, '{emb_lit}'::vector,
                   :source_id, :tags,
                   :object_type, :published_at, :created_at, :doc_kind, :embedding_model)
                """,
                {
                    "id": node_id, "uid": user_id,
                    "title": str(m.get("title") or node_id),
                    "abstract": abstract,
                    "source_id": source_id,
                    "tags": tags,
                    "object_type": object_type,
                    "published_at": published_at,
                    "created_at": created_at,
                    "doc_kind": m.get("doc_kind") or config_loader.get("doc_kind.default", "other"),
                    "embedding_model": config_loader.get("embedding.model", "text-embedding-3-small"),
                },
            )
            await object_nodes.upsert_object_node(
                node_id,
                object_type,
                {
                    "source_item_id": None,
                    "raw_ref": raw_ref_dict,
                    "source_type": source_type,
                    "tags": tags,
                    "summary_of": summary_of,
                    "perspective_label": perspective or "default",
                    "perspective_instruction": perspective or "默认摘要",
                    "body": body or abstract,
                    "is_default": not bool(perspective),
                    "source": {"source_node_ids": source_node_ids, "restored_from_wiki": True},
                    "canonical_name": canonical_name,
                    "aliases": aliases,
                    "description": abstract,
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

        # summarizes 关系由 summary_nodes.summary_of FK 表达，不在此处建边

        # legacy part_of relations are restored into index_children, not knowledge_edges.
        relations = m.get("relations") or []
        if isinstance(relations, list):
            for rel in relations:
                if isinstance(rel, dict) and rel.get("type") == "part_of" and rel.get("id"):
                    try:
                        await index_structure.add_child(
                            rel["id"],
                            node_id,
                            user_id=user_id,
                            child_role="member",
                        )
                    except Exception as e:
                        print(f"[restore] index child error {rel['id']}→{node_id}: {e}", flush=True)

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

def _parse_rebuild_time(value: str | None):
    if not value:
        return None
    from datetime import datetime as _datetime

    return _datetime.fromisoformat(value.replace("Z", "+00:00"))


async def rebuild_from_raw(
    user_id: str = USER_ID,
    *,
    source_id: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    dry_run: bool = False,
    resume: bool = False,
) -> dict:
    """
    从 source_items manifest 重建知识库（幂等）。
    执行流程：
      1. 按 source_items manifest 选择待重建 item（支持 source/type/status/time filter）
      2. 删除对应 wiki 文件
      3. 将选中 source_items 重置为 pending，触发 ingestion-worker 重新处理
      4. 轮询等待选中 source_items 完成（最长 60 分钟）
      5. 运行 run_maintenance()

    须在 api 容器中执行：
      docker compose exec api python maintenance.py rebuild_from_raw --confirm
    """
    import pathlib as _pathlib
    import httpx

    user_data_dir = _pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
    wiki_dir = user_data_dir / user_id / "wiki"
    ingestion_url = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")

    filters = {
        "source_id": source_id,
        "source_type": source_type,
        "status": status,
        "since": since,
        "until": until,
        "resume": resume,
        "dry_run": dry_run,
    }

    # ── Step 1: 选择 manifest items ───────────────────────────────────────────
    print(f"[rebuild] Step 1: 选择 source_items manifest... {filters}", flush=True)

    where = ["si.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id}
    if source_id:
        where.append("si.source_id = :source_id")
        params["source_id"] = source_id
    if source_type:
        where.append("si.source_type = :source_type")
        params["source_type"] = source_type
    if status:
        where.append("si.status = :status")
        params["status"] = status
    if since:
        where.append(
            "COALESCE(si.effective_at, si.source_published_at, si.captured_at, si.created_at) >= :since"
        )
        params["since"] = _parse_rebuild_time(since)
    if until:
        where.append(
            "COALESCE(si.effective_at, si.source_published_at, si.captured_at, si.created_at) <= :until"
        )
        params["until"] = _parse_rebuild_time(until)
    if resume:
        where.append("si.status <> 'succeeded'")

    item_rows = await database.database.fetch_all(
        f"""
        SELECT si.id, si.source_id, si.source_type, si.status
        FROM source_items si
        WHERE {' AND '.join(where)}
        ORDER BY si.source_id, si.created_at ASC
        """,
        params,
    )
    item_ids = [r["id"] for r in item_rows]
    source_ids = sorted({r["source_id"] for r in item_rows})
    source_types = sorted({r["source_type"] for r in item_rows})

    if not item_ids:
        result = {
            "dry_run": dry_run,
            "filters": filters,
            "source_items_selected": 0,
            "sources_selected": 0,
            "nodes_deleted": 0,
            "wiki_files_deleted": 0,
            "sources_triggered": 0,
            "sources_failed": 0,
            "maintenance": None,
        }
        print(f"[rebuild] 无匹配 source_items: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return result

    node_rows = await database.database.fetch_all(
        """
        SELECT n.id, n.object_type
        FROM knowledge_nodes n
        JOIN article_nodes an ON an.node_id = n.id
        WHERE n.user_id = :uid
          AND an.source_item_id = ANY(:item_ids)
          AND n.object_type = 'article'
        """,
        {"uid": user_id, "item_ids": item_ids},
    )
    base_ids = {r["id"] for r in node_rows}
    summary_rows = await database.database.fetch_all(
        """
        SELECT n.id
        FROM knowledge_nodes n
        JOIN summary_nodes sn ON sn.node_id = n.id
        WHERE n.user_id = :uid
          AND n.object_type = 'summary'
          AND sn.summary_of = ANY(:base_ids)
        """,
        {"uid": user_id, "base_ids": list(base_ids) or ["__none__"]},
    )
    summary_ids = {r["id"] for r in summary_rows}
    entity_rows = await database.database.fetch_all(
        """
        SELECT DISTINCT n.id
        FROM knowledge_nodes n
        LEFT JOIN entity_facts ef ON ef.entity_id = n.id
        LEFT JOIN knowledge_edges ke
          ON ke.to_node_id = n.id
         AND ke.relation_type IN ('mentions', 'wikilink')
        WHERE n.user_id = :uid
          AND n.object_type = 'entity'
          AND (
            ef.article_id = ANY(:base_ids)
            OR ke.from_node_id = ANY(:base_ids)
          )
        """,
        {"uid": user_id, "base_ids": list(base_ids) or ["__none__"]},
    )
    entity_ids = {r["id"] for r in entity_rows}
    deleted_ids = base_ids | summary_ids | entity_ids

    dry_run_result = {
        "dry_run": True,
        "filters": filters,
        "source_items_selected": len(item_ids),
        "sources_selected": len(source_ids),
        "source_types_selected": source_types,
        "nodes_to_delete": len(deleted_ids),
        "article_or_index_nodes_to_delete": len(base_ids),
        "summary_nodes_to_delete": len(summary_ids),
        "entity_nodes_to_delete": len(entity_ids),
    }
    if dry_run:
        print(f"[rebuild] dry run: {json.dumps(dry_run_result, ensure_ascii=False)}", flush=True)
        return dry_run_result

    # ── Step 2: 清空可重建内容 ────────────────────────────────────────────────
    print("[rebuild] Step 2: 清空选中 manifest 对应内容...", flush=True)

    ec_before_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM entity_candidates WHERE user_id = :uid",
        {"uid": user_id},
    )
    ec_before = int(ec_before_row["n"]) if ec_before_row else 0
    await database.database.execute(
        """
        DELETE FROM entity_candidates
        WHERE user_id = :uid
          AND source_article_ids && CAST(:base_ids AS text[])
        """,
        {"uid": user_id, "base_ids": list(base_ids) or ["__none__"]},
    )
    ec_after_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM entity_candidates WHERE user_id = :uid",
        {"uid": user_id},
    )
    ec_after = int(ec_after_row["n"]) if ec_after_row else 0
    ec_deleted = ec_before - ec_after

    if deleted_ids:
        await database.database.execute(
            """
            DELETE FROM knowledge_nodes
            WHERE user_id = :uid AND id = ANY(:ids)
            """,
            {"uid": user_id, "ids": list(deleted_ids)},
        )

    print(
        f"[rebuild] 已清空: nodes={len(deleted_ids)}, "
        f"base={len(base_ids)}, summaries={len(summary_ids)}, entities={len(entity_ids)}, "
        f"entity_candidates_deleted={ec_deleted}",
        flush=True,
    )

    # ── Step 2b: 清理 wiki 文件 ───────────────────────────────────────────────
    wiki_deleted = 0

    # 按 ID 删除 article/entity/index/summary 的 wiki 文件
    for subdir in ("articles", "entities", "indices", "summaries"):
        d = wiki_dir / subdir
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and f.suffix == ".md" and f.stem in deleted_ids:
                f.unlink()
                wiki_deleted += 1

    print(f"[rebuild] 已删除 wiki 文件: {wiki_deleted} 个", flush=True)

    # ── Step 3: 重置 source_items，触发 ingestion-worker ────────────────────
    print("[rebuild] Step 3: 触发 ingestion-worker...", flush=True)

    await database.database.execute(
        """
        UPDATE source_items
        SET status = 'pending', error = NULL, attempts = 0, updated_at = NOW()
        WHERE user_id = :uid AND id = ANY(:item_ids)
        """,
        {"uid": user_id, "item_ids": item_ids},
    )
    await database.database.execute(
        """
        UPDATE sources
        SET last_fetched_at = NULL
        WHERE user_id = :uid AND id = ANY(:source_ids)
        """,
        {"uid": user_id, "source_ids": source_ids},
    )

    sources = await database.database.fetch_all(
        """
        SELECT id, name
        FROM sources
        WHERE user_id = :uid AND id = ANY(:source_ids)
        ORDER BY created_at ASC
        """,
        {"uid": user_id, "source_ids": source_ids},
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
            f"[rebuild] Step 4: 等待 {len(item_ids)} 个 source_items 完成（最长 60 分钟）...",
            flush=True,
        )
        max_wait = config_loader.get("maintenance.rebuild_max_wait_seconds", 3600)
        interval = config_loader.get("maintenance.rebuild_poll_interval_seconds", 20)
        elapsed = 0
        pending_count = len(item_ids)

        while elapsed < max_wait and pending_count:
            await asyncio.sleep(interval)
            elapsed += interval
            row = await database.database.fetch_one(
                """
                SELECT COUNT(*) AS n
                FROM source_items
                WHERE user_id = :uid
                  AND id = ANY(:item_ids)
                  AND status IN ('pending', 'processing')
                """,
                {"uid": user_id, "item_ids": item_ids},
            )
            still_count = int(row["n"]) if row else 0
            if still_count < pending_count:
                done = len(item_ids) - still_count
                print(
                    f"[rebuild]   进度: {done}/{len(item_ids)} 完成 ({elapsed}s 已过)",
                    flush=True,
                )
            pending_count = still_count

        if pending_count:
            print(f"[rebuild] 警告：超时，仍有 {pending_count} 个 source_items 未完成", flush=True)
        else:
            print("[rebuild] 所有选中 source_items 已完成 ingestion", flush=True)

    # ── Step 5: 运行维护任务 ──────────────────────────────────────────────────
    print("[rebuild] Step 5: 运行维护任务（entity 晋升、wikilink 回灌等）...", flush=True)
    maintenance_result = await run_maintenance(user_id)

    result = {
        "entity_candidates_deleted": ec_deleted,
        "entities_deleted": len(entity_ids),
        "article_or_index_nodes_deleted": len(base_ids),
        "summary_nodes_deleted": len(summary_ids),
        "source_items_selected": len(item_ids),
        "sources_selected": len(source_ids),
        "source_types_selected": source_types,
        "wiki_files_deleted": wiki_deleted,
        "sources_triggered": len(triggered),
        "sources_failed": len(failed),
        "filters": filters,
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

    facts_result = await entity_insights.backfill_entity_facts_from_mentions(user_id)
    print(f"[maintenance] Entity facts backfill: {facts_result}", flush=True)

    # entity_profiles 表已删除；entity 描述统一回到 nodes.abstract（regenerate 端点按需更新）
    relatedness_result = await entity_insights.rebuild_entity_pair_signals(user_id)
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


if __name__ == "__main__":
    async def main():
        await database.init()
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""
        if cmd == "restore_from_wiki":
            result = await restore_from_wiki()
        elif cmd == "rebuild_from_raw":
            if "--confirm" not in sys.argv:
                print(
                    "此操作将按 source_items manifest 清空可重建派生节点"
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

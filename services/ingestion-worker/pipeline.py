"""
Ingestion 流水线（所有 source 类型共用）：
  fetch → extract_text → save_raw
    → get_analysis_context (API: nearby entities + top candidates)
    → analyze_article (Claude: abstract + tags + entity candidates)
    → embed → post_ingest(article) → post_ingest(summary)
    → process_entity_candidates (API)
    → for each promoted candidate: generate_entity_page (Claude) → post_ingest(entity)
    → write wiki files
    → update_last_fetched
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from openai import AsyncOpenAI

import config_loader
import prompt_loader
from sources.base import BaseSource, RawItem

logger = logging.getLogger(__name__)

API_BASE_URL = os.environ["API_BASE_URL"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

MAX_TEXT_CHARS = config_loader.get("ingestion.max_text_chars", 12000)
MAX_ENTITY_PAGE_SOURCES = config_loader.get("ingestion.max_entity_page_sources", 5)


def _infer_title_from_text(text: str) -> str | None:
    first_nonempty: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if first_nonempty is None:
            first_nonempty = stripped
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:120]
    if first_nonempty is None:
        return None
    if len(first_nonempty) <= 80 and not first_nonempty[-1] in ("。", ".", "，", ",", "；", ";"):
        return first_nonempty
    return first_nonempty[:80].rsplit(" ", 1)[0] or first_nonempty[:80]


def save_raw(item: RawItem, source_type: str) -> str:
    raw_dir = USER_DATA_DIR / USER_ID / "raw" / source_type
    raw_dir.mkdir(parents=True, exist_ok=True)

    file_name = getattr(item, "_file_name", None) or f"{datetime.utcnow().strftime('%Y-%m-%d')}-unknown.html"
    file_path = raw_dir / file_name

    if item.raw_bytes:
        file_path.write_bytes(item.raw_bytes)
    return str(file_path)


def analyze_article(text: str, nearby_entities: list[dict], top_candidates: list[dict]) -> dict:
    """
    Call Claude to analyze an article.
    Returns: {abstract, tags, entities, contradictions, structural_hints}
    """
    truncated = text[:MAX_TEXT_CHARS]

    existing_entities_str = "\n".join(
        f"- {e['title']} (id: {e['id']})" for e in nearby_entities
    ) or "（暂无）"

    candidate_entities_str = "\n".join(
        f"- {c['canonical_name']} (已出现 {c['mention_count']} 次)" for c in top_candidates
    ) or "（暂无）"

    message = claude.messages.create(
        model=config_loader.get("models.article_analysis", "claude-haiku-4-5-20251001"),
        max_tokens=config_loader.get("llm_output_tokens.article_analysis", 2048),
        messages=[
            {
                "role": "user",
                "content": prompt_loader.fill(
                    "article_analysis",
                    text=truncated,
                    existing_entities=existing_entities_str,
                    candidate_entities=candidate_entities_str,
                ),
            }
        ],
    )
    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: extract abstract and tags via regex
        abstract = ""
        tags: list[str] = []
        m = re.search(r'"abstract"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            abstract = m.group(1)
        m = re.search(r'"tags"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
        if m:
            tags = re.findall(r'"([^"]*)"', m.group(1))
        data = {"abstract": abstract, "tags": tags, "entities": [], "contradictions": [], "structural_hints": []}

    return {
        "abstract": data.get("abstract", ""),
        "tags": data.get("tags", []),
        "entities": data.get("entities", []),
        "contradictions": data.get("contradictions", []),
        "structural_hints": data.get("structural_hints", []),
    }


def generate_entity_page(canonical_name: str, aliases: list[str], source_abstracts: list[str]) -> str:
    """Call Claude to generate a Wikipedia-style entity page body (markdown)."""
    message = claude.messages.create(
        model=config_loader.get("models.entity_page", "claude-haiku-4-5-20251001"),
        max_tokens=config_loader.get("llm_output_tokens.entity_page", 2048),
        messages=[
            {
                "role": "user",
                "content": prompt_loader.fill(
                    "entity_page",
                    entity_name=canonical_name,
                    aliases="、".join(aliases) if aliases else "无",
                    source_abstracts="\n\n".join(source_abstracts) or "（暂无来源信息）",
                ),
            }
        ],
    )
    return message.content[0].text.strip()


async def embed(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model=config_loader.get("embedding.model", "text-embedding-3-small"),
        input=text[:config_loader.get("embedding.max_chars", 8000)],
        dimensions=config_loader.get("embedding.dimensions", 1536),
    )
    return resp.data[0].embedding


async def post_ingest(payload: dict) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{API_BASE_URL}/api/kb/ingest", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["id"]


async def _post_ingest_full(payload: dict) -> dict:
    """Like post_ingest but returns the full response dict (includes 'duplicate' flag)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{API_BASE_URL}/api/kb/ingest", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()


async def get_analysis_context(embedding: list[float]) -> dict:
    """Fetch nearby entity titles and top candidates from API."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/kb/entity_candidates/analyze_context",
            json={"embedding": embedding},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    return {"nearby_entities": [], "top_candidates": []}


async def process_entity_candidates(article_id: str, entities: list[dict]) -> dict:
    """Send entity candidate list to API for DB processing. Returns promoted candidates."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/kb/entity_candidates/process",
            json={"article_id": article_id, "entities": entities},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    return {"matched_existing": [], "promoted": []}


async def mark_candidate_promoted(candidate_id: int, entity_node_id: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{API_BASE_URL}/api/kb/entity_candidates/{candidate_id}/mark_promoted",
            json={"entity_node_id": entity_node_id},
            timeout=10,
        )


async def update_last_fetched(source_id: str):
    now = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient() as client:
        await client.put(
            f"{API_BASE_URL}/api/sources/{source_id}",
            json={"last_fetched_at": now},
            timeout=10,
        )


def write_wiki_article(node_id: str, item: RawItem, text: str, tags: list[str], raw_ref: dict,
                        title_override: str | None = None, source_type_override: str | None = None):
    """Write wiki/articles/{node_id}.md with full cleaned article text."""
    title = title_override or item.title or "（无标题）"
    source_type = source_type_override or item.raw_ref.get('type', 'unknown')
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "articles"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    raw_ref_path = raw_ref.get("path") or raw_ref.get("url", "")
    tags_yaml = "[" + ", ".join(tags) + "]"
    created = item.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    content = f"""---
id: {node_id}
type: article
title: "{title}"
tags: {tags_yaml}
wikilinks: []
source_type: {source_type}
raw_ref: {raw_ref_path}
created_at: {created}
updated_at: {created}
---

# {title}

{text}
"""
    (wiki_dir / f"{node_id}.md").write_text(content, encoding="utf-8")


def write_wiki_summary(summary_id: str, article_id: str, article_title: str,
                        abstract: str, tags: list[str], created: str):
    """Write wiki/summaries/{summary_id}.md."""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "summaries"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    tags_yaml = "[" + ", ".join(tags) + "]"

    content = f"""---
id: {summary_id}
type: summary
title: "摘要：{article_title}"
tags: {tags_yaml}
wikilinks: []
summary_of: {article_id}
sources: [{article_id}]
created_at: {created}
updated_at: {created}
---

# 摘要：{article_title}

{abstract}
"""
    (wiki_dir / f"{summary_id}.md").write_text(content, encoding="utf-8")


def write_wiki_entity(entity_id: str, canonical_name: str, aliases: list[str],
                       source_ids: list[str], body: str, tags: list[str]):
    """Write wiki/entities/{entity_id}.md."""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "entities"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    tags_yaml = "[" + ", ".join(tags) + "]"
    aliases_yaml = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    sources_yaml = "[" + ", ".join(source_ids) + "]"
    created = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    content = f"""---
id: {entity_id}
type: entity
title: "{canonical_name}"
tags: {tags_yaml}
wikilinks: []
canonical_name: {canonical_name}
aliases: {aliases_yaml}
sources: {sources_yaml}
created_at: {created}
updated_at: {created}
---

# {canonical_name}

{body}
"""
    (wiki_dir / f"{entity_id}.md").write_text(content, encoding="utf-8")


async def run_pipeline(source: BaseSource, source_config: dict):
    source_id = source_config["id"]
    source_type = source_config["type"]

    last_fetched_at = source_config.get("last_fetched_at")
    if last_fetched_at:
        if isinstance(last_fetched_at, str):
            last_fetched_at = datetime.fromisoformat(last_fetched_at.replace("Z", "+00:00"))

    logger.info(f"[{source_id}] 开始抓取，last_fetched_at={last_fetched_at}")
    items: list[RawItem] = source.fetch_new_items(last_fetched_at)
    logger.info(f"[{source_id}] 获取到 {len(items)} 条新内容")

    for item in items:
        try:
            # 1. Extract text
            text = source.extract_text(item)
            if not text or len(text) < 50:
                logger.warning(f"[{source_id}] 跳过，正文过短: {item.title}")
                continue

            if item.raw_ref.get("type") == "file":
                stem = Path(item.raw_ref["path"]).stem
                if not item.title or item.title == stem:
                    inferred = _infer_title_from_text(text)
                    if inferred:
                        item.title = inferred

            # 2. Save raw file
            file_path = save_raw(item, source_type)
            if item.raw_ref.get("type") == "url":
                raw_ref = {"type": "url", "url": item.raw_ref["url"], "cached": file_path}
            else:
                raw_ref = {"type": "file", "path": file_path}

            # 3. Initial embedding of raw text (used for entity context lookup)
            initial_embedding = await embed(text[:8000])

            # 4. Get entity analysis context from API
            context = await get_analysis_context(initial_embedding)
            nearby_entities = context.get("nearby_entities", [])
            top_candidates = context.get("top_candidates", [])

            # 5. Analyze article: abstract + tags + entity candidates
            analysis = analyze_article(text, nearby_entities, top_candidates)
            abstract = analysis["abstract"]
            tags = analysis["tags"]
            entities = analysis["entities"]
            logger.info(f"[{source_id}] 分析完成: {item.title} | entities={len(entities)}")

            # 6. Embed abstract for storage
            embedding = await embed(abstract) if abstract else initial_embedding

            # 7. Ingest article node
            article_id = await post_ingest({
                "user_id": USER_ID,
                "title": item.title,
                "abstract": abstract,
                "embedding": embedding,
                "source_type": source_type,
                "source_id": source_id,
                "raw_ref": raw_ref,
                "tags": tags,
                "is_primary": source_config.get("is_primary", True),
                "object_type": "article",
            })
            logger.info(f"[{source_id}] article 入库: {article_id} — {item.title}")

            # 8. Ingest summary node (body = abstract, init version)
            summary_embedding = await embed(abstract) if abstract else embedding
            summary_id = await post_ingest({
                "user_id": USER_ID,
                "title": f"摘要：{item.title}",
                "abstract": abstract,
                "embedding": summary_embedding,
                "source_type": source_type,
                "source_id": source_id,
                "raw_ref": {},
                "tags": tags,
                "object_type": "summary",
                "summary_of": article_id,
                "source_node_ids": [article_id],
            })
            logger.info(f"[{source_id}] summary 入库: {summary_id}")

            # 9. Process entity candidates via API
            newly_promoted_entity_ids: list[str] = []
            if entities:
                candidate_result = await process_entity_candidates(article_id, entities)
                promoted_list = candidate_result.get("promoted", [])

                # 10. Generate entity pages for newly promoted candidates
                for promoted in promoted_list:
                    try:
                        # Fetch source article abstracts
                        source_abstracts = []
                        for art_id in promoted.get("source_article_ids", [])[:MAX_ENTITY_PAGE_SOURCES]:
                            async with httpx.AsyncClient() as hc:
                                art_resp = await hc.get(
                                    f"{API_BASE_URL}/api/kb/node/{art_id}", timeout=10
                                )
                                if art_resp.status_code == 200:
                                    art_data = art_resp.json()
                                    if art_data.get("abstract"):
                                        t = art_data.get("title") or art_id
                                        source_abstracts.append(f"《{t}》: {art_data['abstract']}")

                        entity_body = generate_entity_page(
                            promoted["canonical_name"],
                            promoted.get("aliases", []),
                            source_abstracts,
                        )

                        entity_embedding = await embed(promoted["canonical_name"])
                        entity_id = await post_ingest({
                            "user_id": USER_ID,
                            "title": promoted["canonical_name"],
                            "abstract": entity_body[:500],
                            "embedding": entity_embedding,
                            "source_type": "entity",
                            "source_id": source_id,
                            "raw_ref": {},
                            "tags": [],
                            "object_type": "entity",
                            "source_node_ids": promoted.get("source_article_ids", []),
                            "canonical_name": promoted["canonical_name"],
                            "aliases": promoted.get("aliases", []),
                        })

                        write_wiki_entity(
                            entity_id,
                            promoted["canonical_name"],
                            promoted.get("aliases", []),
                            promoted.get("source_article_ids", []),
                            entity_body,
                            [],
                        )

                        await mark_candidate_promoted(promoted["candidate_id"], entity_id)
                        newly_promoted_entity_ids.append(entity_id)
                        logger.info(f"[{source_id}] entity 晋升入库: {entity_id} — {promoted['canonical_name']}")
                    except Exception as e:
                        logger.error(f"[{source_id}] entity 生成失败: {promoted.get('canonical_name')} — {e}", exc_info=True)

            # 11. Write wiki article and summary files
            created_str = item.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            write_wiki_article(article_id, item, text, tags, raw_ref)
            write_wiki_summary(summary_id, article_id, item.title or article_id, abstract, tags, created_str)

            # 12. Backfill wikilinks for each newly promoted entity into all articles
            for eid in newly_promoted_entity_ids:
                try:
                    async with httpx.AsyncClient() as hc:
                        await hc.post(
                            f"{API_BASE_URL}/api/kb/entities/{eid}/backfill_wikilinks",
                            timeout=30,
                        )
                except Exception as e:
                    logger.warning(f"[{source_id}] wikilink backfill failed for {eid}: {e}")

        except Exception as e:
            logger.error(f"[{source_id}] 处理失败: {item.title} — {e}", exc_info=True)

    await update_last_fetched(source_id)
    logger.info(f"[{source_id}] 完成，已更新 last_fetched_at")


async def run_book_pipeline(source, source_config: dict):
    """
    Book ingestion pipeline: parse EPUB/MOBI → create index node + article/summary per chapter.

    Index node uses book file as raw_ref (deduplicated by path).
    Chapter article nodes use a virtual raw_ref path for deterministic ID + dedup.
    After ingestion, run_maintenance()'s aggregate_index_abstracts fills the index abstract.
    """
    from sources.book import BookSource  # local to avoid circular at module level

    source_id = source_config["id"]
    source_type = source_config["type"]

    last_fetched_at = source_config.get("last_fetched_at")
    if last_fetched_at and isinstance(last_fetched_at, str):
        last_fetched_at = datetime.fromisoformat(last_fetched_at.replace("Z", "+00:00"))

    items: list[RawItem] = source.fetch_new_items(last_fetched_at)
    logger.info(f"[{source_id}] book pipeline: {len(items)} file(s) to process")

    for item in items:
        try:
            # 1. Parse chapters
            chapters = source.extract_chapters(item)
            valid_chapters = [ch for ch in chapters if len(ch.text) >= 300]
            if not valid_chapters:
                logger.warning(f"[{source_id}] no valid chapters in: {item.title}")
                continue

            logger.info(f"[{source_id}] {item.title}: {len(valid_chapters)} chapters")

            # 2. Save raw file
            file_path = save_raw(item, source_type)
            raw_ref = {"type": "file", "path": file_path}

            # 3. Create index node for the book (abstract empty; maintenance will fill it)
            book_title = item.title or Path(file_path).stem
            index_embedding = await embed(book_title)
            index_resp = await _post_ingest_full({
                "user_id": USER_ID,
                "title": book_title,
                "abstract": "",
                "embedding": index_embedding,
                "source_type": source_type,
                "source_id": source_id,
                "raw_ref": raw_ref,
                "tags": [],
                "object_type": "index",
            })

            if index_resp.get("duplicate"):
                logger.info(f"[{source_id}] book already ingested (index exists): {book_title}")
                continue

            index_id = index_resp["id"]
            logger.info(f"[{source_id}] index node created: {index_id} — {book_title}")

            # 4. Process each chapter
            for ch in valid_chapters:
                try:
                    truncated = ch.text[:MAX_TEXT_CHARS]

                    # Article analysis (no entity context lookup to keep book ingestion fast)
                    analysis = analyze_article(truncated, [], [])
                    abstract = analysis["abstract"]
                    tags = analysis["tags"]
                    entities = analysis["entities"]

                    embedding = await embed(abstract) if abstract else await embed(truncated[:8000])

                    # Virtual path for deterministic ID + dedup
                    chapter_raw_ref = {
                        "type": "book_chapter",
                        "path": f"{file_path}::chapter::{ch.order}",
                    }

                    article_id = await post_ingest({
                        "user_id": USER_ID,
                        "title": ch.title,
                        "abstract": abstract,
                        "embedding": embedding,
                        "source_type": source_type,
                        "source_id": source_id,
                        "raw_ref": chapter_raw_ref,
                        "tags": tags,
                        "object_type": "article",
                        "parent_index_id": index_id,
                    })
                    logger.info(f"[{source_id}] chapter: {article_id} — {ch.title}")

                    # Write wiki file with actual chapter text (not summary)
                    write_wiki_article(
                        article_id, item, ch.text, tags, chapter_raw_ref,
                        title_override=ch.title,
                        source_type_override="book_chapter",
                    )

                    # Summary node
                    summary_embedding = await embed(abstract) if abstract else embedding
                    await post_ingest({
                        "user_id": USER_ID,
                        "title": f"摘要：{ch.title}",
                        "abstract": abstract,
                        "embedding": summary_embedding,
                        "source_type": source_type,
                        "source_id": source_id,
                        "raw_ref": {},
                        "tags": tags,
                        "object_type": "summary",
                        "summary_of": article_id,
                        "source_node_ids": [article_id],
                    })

                    # Entity candidates (same flow as regular pipeline)
                    newly_promoted: list[str] = []
                    if entities:
                        candidate_result = await process_entity_candidates(article_id, entities)
                        promoted_list = candidate_result.get("promoted", [])

                        for promoted in promoted_list:
                            try:
                                source_abstracts = []
                                for art_id in promoted.get("source_article_ids", [])[:MAX_ENTITY_PAGE_SOURCES]:
                                    async with httpx.AsyncClient() as hc:
                                        art_resp = await hc.get(
                                            f"{API_BASE_URL}/api/kb/node/{art_id}", timeout=10
                                        )
                                        if art_resp.status_code == 200:
                                            art_data = art_resp.json()
                                            if art_data.get("abstract"):
                                                t = art_data.get("title") or art_id
                                                source_abstracts.append(f"《{t}》: {art_data['abstract']}")

                                entity_body = generate_entity_page(
                                    promoted["canonical_name"],
                                    promoted.get("aliases", []),
                                    source_abstracts,
                                )
                                entity_embedding = await embed(promoted["canonical_name"])
                                entity_id = await post_ingest({
                                    "user_id": USER_ID,
                                    "title": promoted["canonical_name"],
                                    "abstract": entity_body[:500],
                                    "embedding": entity_embedding,
                                    "source_type": "entity",
                                    "source_id": source_id,
                                    "raw_ref": {},
                                    "tags": [],
                                    "object_type": "entity",
                                    "source_node_ids": promoted.get("source_article_ids", []),
                                    "canonical_name": promoted["canonical_name"],
                                    "aliases": promoted.get("aliases", []),
                                })
                                write_wiki_entity(
                                    entity_id,
                                    promoted["canonical_name"],
                                    promoted.get("aliases", []),
                                    promoted.get("source_article_ids", []),
                                    entity_body,
                                    [],
                                )
                                await mark_candidate_promoted(promoted["candidate_id"], entity_id)
                                newly_promoted.append(entity_id)
                            except Exception as e:
                                logger.error(f"[{source_id}] entity failed: {promoted.get('canonical_name')} — {e}")

                    # Backfill wikilinks for newly promoted entities
                    for eid in newly_promoted:
                        try:
                            async with httpx.AsyncClient() as hc:
                                await hc.post(
                                    f"{API_BASE_URL}/api/kb/entities/{eid}/backfill_wikilinks",
                                    timeout=30,
                                )
                        except Exception as e:
                            logger.warning(f"[{source_id}] wikilink backfill failed for {eid}: {e}")

                except Exception as e:
                    logger.error(f"[{source_id}] chapter failed: {ch.title} — {e}", exc_info=True)

        except Exception as e:
            logger.error(f"[{source_id}] book failed: {item.title} — {e}", exc_info=True)

    await update_last_fetched(source_id)
    logger.info(f"[{source_id}] book pipeline done")

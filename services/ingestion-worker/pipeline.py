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
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import httpx
import trafilatura
import yaml
from openai import AsyncOpenAI

from article_ingestion import ArticleIngestionAdapters, ArticleIngestionInput, process_article_like_item
from settings import settings
from prompts import prompts
from sources.base import BaseSource, RawItem, message_text

logger = logging.getLogger(__name__)

API_BASE_URL = os.environ["API_BASE_URL"]
KB_SERVICE_TOKEN = os.environ.get("KB_SERVICE_TOKEN", "").strip()
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

MAX_TEXT_CHARS = settings.ingestion.max_text_chars
MAX_ENTITY_PAGE_SOURCES = settings.ingestion.max_entity_page_sources


def _service_headers() -> dict[str, str]:
    """Auth header for internal API calls gated by require_auth_or_service_token."""
    return {"X-KB-Service-Token": KB_SERVICE_TOKEN} if KB_SERVICE_TOKEN else {}


def _message_text(message: Any) -> str:
    return message_text(message)


def _extract_json_object(text: str) -> dict | None:
    """Parse the first JSON object from an LLM reply, tolerating prose and code
    fences (mirrors the defensive cite-path parser). Returns None if none found."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if "```" in text:
            text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


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

    file_name = getattr(item, "_file_name", None)
    if not file_name:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Content-address the fallback name so two items captured the same day
        # don't clobber each other; identical content re-saves to the same path.
        if item.raw_bytes:
            file_name = f"{date_str}-{hashlib.sha256(item.raw_bytes).hexdigest()[:12]}.html"
        else:
            file_name = f"{date_str}-unknown.html"
    file_path = raw_dir / file_name

    if item.raw_bytes:
        file_path.write_bytes(item.raw_bytes)
    return str(file_path)


def _time_payload(item: RawItem) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key in ("source_published_at", "source_updated_at", "captured_at", "effective_at"):
        value = getattr(item, key, None)
        if value:
            payload[key] = value.isoformat()
    return payload


def _parse_item_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def fetch_pending_source_items(source_id: str, limit: int = 100) -> list[dict]:
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.get(
            f"{API_BASE_URL}/api/sources/{source_id}/source-items",
            params={"status": "pending", "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


async def create_source_items(source_id: str, items: list[dict]) -> list[dict]:
    if not items:
        return []
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/sources/{source_id}/source-items",
            json={"items": items},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])


async def update_source_item_status(
    item_id: str,
    status: str,
    *,
    raw_snapshot_ref: str | None = None,
    extracted_text_ref: str | None = None,
    error: str | None = None,
    title: str | None = None,
) -> None:
    payload = {
        "status": status,
        "raw_snapshot_ref": raw_snapshot_ref,
        "extracted_text_ref": extracted_text_ref,
        "error": error,
        "title": title,
    }
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/sources/source-items/{item_id}/status",
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()


def save_extracted_text(source_type: str, source_item_id: str, text: str) -> str:
    extracted_dir = USER_DATA_DIR / USER_ID / "extracted" / source_type
    extracted_dir.mkdir(parents=True, exist_ok=True)
    path = extracted_dir / f"{source_item_id}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _source_item_payload_from_raw(item: RawItem, source_type: str) -> dict:
    origin_ref = item.raw_ref.get("url") or item.raw_ref.get("path") or item.title or item.source_id
    origin_ref_type = "feed_entry" if source_type == "rss" else item.raw_ref.get("type", "external")
    raw_snapshot_ref = None
    content_hash = None
    if item.raw_bytes:
        raw_snapshot_ref = save_raw(item, source_type)
        content_hash = hashlib.sha256(item.raw_bytes).hexdigest()
    elif item.raw_ref.get("path"):
        raw_snapshot_ref = item.raw_ref["path"]
        try:
            content_hash = hashlib.sha256(Path(raw_snapshot_ref).read_bytes()).hexdigest()
        except OSError:
            content_hash = None
    return {
        "origin_ref": origin_ref,
        "origin_ref_type": origin_ref_type,
        "raw_snapshot_ref": raw_snapshot_ref,
        "content_hash": content_hash,
        "title": item.title,
        "source_published_at": item.source_published_at.isoformat() if item.source_published_at else None,
        "source_updated_at": item.source_updated_at.isoformat() if item.source_updated_at else None,
        "captured_at": item.captured_at.isoformat() if item.captured_at else None,
        "effective_at": item.effective_at.isoformat() if item.effective_at else None,
        "raw_retention_policy": "keep_extracted_only" if source_type in ("rss", "url", "wechat") else "keep_raw",
    }


def _raw_item_from_source_item(row: dict, source_type: str) -> RawItem:
    raw_snapshot_ref = row.get("raw_snapshot_ref")
    origin_ref = row.get("origin_ref") or ""
    origin_ref_type = row.get("origin_ref_type") or "external"
    raw_bytes = None
    raw_ref: dict

    if raw_snapshot_ref:
        p = Path(raw_snapshot_ref)
        if origin_ref_type == "upload" or source_type in ("pdf", "image", "plaintext", "word", "epub", "book"):
            raw_ref = {"type": "file", "path": raw_snapshot_ref}
        else:
            raw_ref = {"type": "url", "url": origin_ref}
            if p.exists():
                raw_bytes = p.read_bytes()
        file_name = p.name
    elif origin_ref_type in ("url", "feed_entry") or source_type in ("url", "rss"):
        downloaded = trafilatura.fetch_url(origin_ref)
        if not downloaded:
            raise RuntimeError(f"failed to fetch URL: {origin_ref}")
        raw_bytes = downloaded.encode("utf-8")
        raw_ref = {"type": "url", "url": origin_ref}
        file_name = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{hashlib.md5(origin_ref.encode()).hexdigest()[:8]}.html"
    else:
        raise RuntimeError("source item has no usable raw snapshot")

    item = RawItem(
        source_id=row["source_id"],
        title=row.get("title"),
        raw_ref=raw_ref,
        content_type="text/html" if raw_ref.get("type") == "url" else "application/octet-stream",
        raw_bytes=raw_bytes,
        fetched_at=_parse_item_time(row.get("effective_at"))
        or _parse_item_time(row.get("source_published_at"))
        or _parse_item_time(row.get("captured_at"))
        or datetime.now(timezone.utc),
        source_published_at=_parse_item_time(row.get("source_published_at")),
        source_updated_at=_parse_item_time(row.get("source_updated_at")),
        captured_at=_parse_item_time(row.get("captured_at")),
        effective_at=_parse_item_time(row.get("effective_at")),
        source_item_id=row["id"],
        document_instance_id=row.get("document_instance_id"),
    )
    item._file_name = file_name
    return item


async def materialize_fetched_source_items(source: BaseSource, source_config: dict, last_fetched_at: datetime | None) -> list[dict]:
    source_id = source_config["id"]
    source_type = source_config["type"]
    items: list[RawItem] = source.fetch_new_items(last_fetched_at)
    logger.info(f"[{source_id}] 获取到 {len(items)} 条新内容")
    payloads = [_source_item_payload_from_raw(item, source_type) for item in items]
    return await create_source_items(source_id, payloads)


def analyze_article(
    text: str,
    nearby_entities: list[dict],
    top_candidates: list[dict],
    popular_tags: list[dict] | None = None,
) -> dict:
    """
    Call Claude to analyze an article.
    Returns: {abstract, tags, entities, contradictions, structural_hints}

    popular_tags 用于 tag 收敛——以"标签 (频次)"形式注入 prompt，
    引导 LLM 优先复用已有标签而非创造同义新词。
    """
    truncated = text[:MAX_TEXT_CHARS]

    existing_entities_str = "\n".join(
        f"- {e['title']} (id: {e['id']})" for e in nearby_entities
    ) or "（暂无）"

    candidate_entities_str = "\n".join(
        f"- {c['canonical_name']} (已出现 {c['mention_count']} 次)" for c in top_candidates
    ) or "（暂无）"

    existing_tags_str = "\n".join(
        f"- {t['tag']} ({t['freq']})" for t in (popular_tags or [])
    ) or "（库中暂无 tag）"

    message = claude.messages.create(
        model=settings.models.article_analysis,
        max_tokens=settings.llm_output_tokens.article_analysis,
        messages=[
            {
                "role": "user",
                "content": prompts.article_analysis(
                    text=truncated,
                    existing_entities=existing_entities_str,
                    candidate_entities=candidate_entities_str,
                    existing_tags=existing_tags_str,
                ),
            }
        ],
    )
    raw = _message_text(message)

    data = _extract_json_object(raw)
    if data is None:
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
        model=settings.models.entity_page,
        max_tokens=settings.llm_output_tokens.entity_page,
        messages=[
            {
                "role": "user",
                "content": prompts.entity_page(
                    entity_name=canonical_name,
                    aliases="、".join(aliases) if aliases else "无",
                    source_abstracts="\n\n".join(source_abstracts) or "（暂无来源信息）",
                ),
            }
        ],
    )
    return _message_text(message)


async def embed(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model=settings.embedding.model,
        input=text[:settings.embedding.max_chars],
        dimensions=settings.embedding.dimensions,
    )
    return resp.data[0].embedding


async def post_ingest(payload: dict) -> str:
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.post(f"{API_BASE_URL}/api/kb/ingest", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["id"]


async def refresh_stale_entities() -> None:
    """Pipeline 结束后调用，触发 API 批量刷新 abstract_stale=true 的 entity。"""
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        try:
            resp = await client.post(
                f"{API_BASE_URL}/api/kb/entities/refresh_stale",
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("stale_found", 0) > 0:
                logger.info("[entity-refresh] %s", result)
        except Exception as exc:
            logger.warning("[entity-refresh] failed: %s", exc)


async def _post_ingest_full(payload: dict) -> dict:
    """Like post_ingest but returns the full response dict (includes 'duplicate' flag)."""
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.post(f"{API_BASE_URL}/api/kb/ingest", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()


async def get_analysis_context(embedding: list[float]) -> dict:
    """Fetch nearby entity titles and top candidates from API."""
    async with httpx.AsyncClient(headers=_service_headers()) as client:
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
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/kb/entity_candidates/process",
            json={"article_id": article_id, "entities": entities},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    return {"matched_existing": [], "promoted": []}


async def get_node(node_id: str) -> dict | None:
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        resp = await client.get(f"{API_BASE_URL}/api/kb/node/{node_id}", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    return None


async def mark_candidate_promoted(candidate_id: int, entity_node_id: str):
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        await client.post(
            f"{API_BASE_URL}/api/kb/entity_candidates/{candidate_id}/mark_promoted",
            json={"entity_node_id": entity_node_id},
            timeout=10,
        )


async def backfill_wikilinks(entity_id: str) -> None:
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        await client.post(
            f"{API_BASE_URL}/api/kb/entities/{entity_id}/backfill_wikilinks",
            timeout=30,
        )


async def update_last_fetched(source_id: str):
    now = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient(headers=_service_headers()) as client:
        await client.put(
            f"{API_BASE_URL}/api/sources/{source_id}",
            json={"last_fetched_at": now},
            timeout=10,
        )


def _wiki_document(frontmatter: dict, title: str, body: str) -> str:
    """Assemble a wiki markdown file: YAML frontmatter + heading + body.

    Frontmatter is serialized with yaml.safe_dump so titles/tags/aliases that
    contain quotes, colons, newlines, brackets or '---' can't corrupt the file.
    """
    fm = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096
    )
    heading = (title or "").replace("\n", " ").strip()
    return f"---\n{fm}---\n\n# {heading}\n\n{body}\n"


def write_wiki_article(node_id: str, item: RawItem, text: str, tags: list[str], raw_ref: dict,
                        doc_kind: str,
                        title_override: str | None = None, source_type_override: str | None = None):
    """Write wiki/articles/{node_id}.md with full cleaned article text."""
    title = title_override or item.title or "（无标题）"
    source_type = source_type_override or item.raw_ref.get('type', 'unknown')
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "articles"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    raw_ref_path = raw_ref.get("path") or raw_ref.get("url", "")
    created = item.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    frontmatter = {
        "id": node_id,
        "type": "article",
        "title": title,
        "tags": list(tags),
        "doc_kind": doc_kind,
        "wikilinks": [],
        "source_type": source_type,
        "raw_ref": raw_ref_path,
        "created_at": created,
        "source_published_at": item.source_published_at.isoformat() if item.source_published_at else "",
        "source_updated_at": item.source_updated_at.isoformat() if item.source_updated_at else "",
        "captured_at": item.captured_at.isoformat() if item.captured_at else "",
        "effective_at": item.effective_at.isoformat() if item.effective_at else "",
        "updated_at": created,
    }
    content = _wiki_document(frontmatter, title, text)
    (wiki_dir / f"{node_id}.md").write_text(content, encoding="utf-8")


def write_wiki_summary(summary_id: str, article_id: str, article_title: str,
                        abstract: str, tags: list[str], created: str):
    """Write wiki/summaries/{summary_id}.md."""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "summaries"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    summary_title = f"摘要：{article_title}"
    frontmatter = {
        "id": summary_id,
        "type": "summary",
        "title": summary_title,
        "tags": list(tags),
        "wikilinks": [],
        "summary_of": article_id,
        "sources": [article_id],
        "created_at": created,
        "updated_at": created,
    }
    content = _wiki_document(frontmatter, summary_title, abstract)
    (wiki_dir / f"{summary_id}.md").write_text(content, encoding="utf-8")


def write_wiki_entity(entity_id: str, canonical_name: str, aliases: list[str],
                       source_ids: list[str], body: str, tags: list[str]):
    """Write wiki/entities/{entity_id}.md."""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "entities"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    frontmatter = {
        "id": entity_id,
        "type": "entity",
        "title": canonical_name,
        "tags": list(tags),
        "wikilinks": [],
        "canonical_name": canonical_name,
        "aliases": list(aliases),
        "sources": list(source_ids),
        "created_at": created,
        "updated_at": created,
    }
    content = _wiki_document(frontmatter, canonical_name, body)
    (wiki_dir / f"{entity_id}.md").write_text(content, encoding="utf-8")


def _article_ingestion_adapters() -> ArticleIngestionAdapters:
    return ArticleIngestionAdapters(
        analyze_article=analyze_article,
        embed=embed,
        post_ingest=post_ingest,
        get_analysis_context=get_analysis_context,
        process_entity_candidates=process_entity_candidates,
        fetch_node=get_node,
        generate_entity_page=generate_entity_page,
        mark_candidate_promoted=mark_candidate_promoted,
        backfill_wikilinks=backfill_wikilinks,
        write_wiki_article=write_wiki_article,
        write_wiki_summary=write_wiki_summary,
        write_wiki_entity=write_wiki_entity,
        max_entity_page_sources=MAX_ENTITY_PAGE_SOURCES,
        embedding_model=settings.embedding.model,
    )


async def run_pipeline(source: BaseSource, source_config: dict):
    source_id = source_config["id"]
    source_type = source_config["type"]

    last_fetched_at = source_config.get("last_fetched_at")
    if last_fetched_at:
        if isinstance(last_fetched_at, str):
            last_fetched_at = datetime.fromisoformat(last_fetched_at.replace("Z", "+00:00"))

    logger.info(f"[{source_id}] 开始抓取，last_fetched_at={last_fetched_at}")
    pending_items = await fetch_pending_source_items(source_id)
    if not pending_items:
        await materialize_fetched_source_items(source, source_config, last_fetched_at)
        pending_items = await fetch_pending_source_items(source_id)
    logger.info(f"[{source_id}] 待处理 source item: {len(pending_items)}")

    for source_item in pending_items:
        item_title = source_item.get("title") or source_item.get("origin_ref") or source_item["id"]
        try:
            await update_source_item_status(source_item["id"], "processing")
            item = _raw_item_from_source_item(source_item, source_type)

            # 1. Extract text
            text = source.extract_text(item)
            if not text or len(text) < 50:
                logger.warning(f"[{source_id}] 跳过，正文过短: {item.title}")
                await update_source_item_status(
                    source_item["id"],
                    "failed",
                    error="extracted text is shorter than 50 characters",
                    title=item.title,
                )
                continue
            extracted_text_ref = save_extracted_text(source_type, source_item["id"], text)

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

            doc_kind = (
                source_item.get("doc_kind")
                or source_config.get("default_doc_kind")
                or settings.doc_kind.default
            )
            result = await process_article_like_item(
                ArticleIngestionInput(
                    user_id=USER_ID,
                    source_id=source_id,
                    source_type=source_type,
                    source_item_id=source_item["id"],
                    document_instance_id=source_item.get("document_instance_id"),
                    item=item,
                    title=item.title,
                    text=text,
                    raw_ref=raw_ref,
                    time_payload=_time_payload(item),
                    use_entity_context=True,
                    doc_kind=doc_kind,
                ),
                _article_ingestion_adapters(),
            )
            logger.info(f"[{source_id}] 分析完成: {item.title} | entities={len(result.entities)}")
            logger.info(f"[{source_id}] article 入库: {result.article_id} — {item.title}")
            logger.info(f"[{source_id}] summary 入库: {result.summary_id}")

            await update_source_item_status(
                source_item["id"],
                "succeeded",
                raw_snapshot_ref=raw_ref.get("path") or raw_ref.get("cached"),
                extracted_text_ref=extracted_text_ref,
                title=item.title,
            )

        except Exception as e:
            logger.error(f"[{source_id}] 处理失败: {item_title} — {e}", exc_info=True)
            try:
                await update_source_item_status(source_item["id"], "failed", error=str(e)[:2000])
            except Exception:
                logger.warning(f"[{source_id}] source item 状态更新失败: {source_item['id']}")

    await update_last_fetched(source_id)
    logger.info(f"[{source_id}] 完成，已更新 last_fetched_at")
    await refresh_stale_entities()


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

    pending_items = await fetch_pending_source_items(source_id)
    if not pending_items:
        await materialize_fetched_source_items(source, source_config, last_fetched_at)
        pending_items = await fetch_pending_source_items(source_id)
    logger.info(f"[{source_id}] book pipeline source items: {len(pending_items)}")

    for source_item in pending_items:
        item_title = source_item.get("title") or source_item.get("origin_ref") or source_item["id"]
        try:
            await update_source_item_status(source_item["id"], "processing")
            item = _raw_item_from_source_item(source_item, source_type)

            # 1. Parse chapters
            chapters = source.extract_chapters(item)
            valid_chapters = [ch for ch in chapters if len(ch.text) >= 300]
            if not valid_chapters:
                logger.warning(f"[{source_id}] no valid chapters in: {item.title}")
                await update_source_item_status(
                    source_item["id"],
                    "failed",
                    error="no valid chapters found",
                    title=item.title,
                )
                continue
            extracted_text_ref = save_extracted_text(
                source_type,
                source_item["id"],
                "\n\n".join(ch.text for ch in valid_chapters),
            )

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
                "source_item_id": source_item["id"],
                **_time_payload(item),
            })

            if index_resp.get("duplicate"):
                logger.info(f"[{source_id}] book already ingested (index exists): {book_title}")
                await update_source_item_status(
                    source_item["id"],
                    "succeeded",
                    raw_snapshot_ref=file_path,
                    extracted_text_ref=extracted_text_ref,
                    title=book_title,
                )
                continue

            index_id = index_resp["id"]
            logger.info(f"[{source_id}] index node created: {index_id} — {book_title}")

            # 4. Process each chapter
            for ch in valid_chapters:
                try:
                    truncated = ch.text[:MAX_TEXT_CHARS]
                    chapter_raw_ref = {
                        "type": "book_chapter",
                        "path": f"{file_path}::chapter::{ch.order}",
                    }

                    chapter_doc_kind = (
                        source_item.get("doc_kind")
                        or source_config.get("default_doc_kind")
                        or settings.doc_kind.default
                    )
                    result = await process_article_like_item(
                        ArticleIngestionInput(
                            user_id=USER_ID,
                            source_id=source_id,
                            source_type=source_type,
                            source_item_id=source_item["id"],
                            item=item,
                            title=ch.title,
                            text=ch.text,
                            raw_ref=chapter_raw_ref,
                            time_payload=_time_payload(item),
                            parent_index_id=index_id,
                            analysis_text=truncated,
                            use_entity_context=False,
                            wiki_source_type="book_chapter",
                            write_summary_wiki=False,
                            doc_kind=chapter_doc_kind,
                        ),
                        _article_ingestion_adapters(),
                    )
                    logger.info(f"[{source_id}] chapter: {result.article_id} — {ch.title}")

                except Exception as e:
                    logger.error(f"[{source_id}] chapter failed: {ch.title} — {e}", exc_info=True)

            await update_source_item_status(
                source_item["id"],
                "succeeded",
                raw_snapshot_ref=file_path,
                extracted_text_ref=extracted_text_ref,
                title=book_title,
            )

        except Exception as e:
            logger.error(f"[{source_id}] book failed: {item_title} — {e}", exc_info=True)
            try:
                await update_source_item_status(source_item["id"], "failed", error=str(e)[:2000])
            except Exception:
                logger.warning(f"[{source_id}] source item 状态更新失败: {source_item['id']}")

    await update_last_fetched(source_id)
    logger.info(f"[{source_id}] book pipeline done")
    await refresh_stale_entities()

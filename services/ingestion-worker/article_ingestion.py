import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sources.base import RawItem

logger = logging.getLogger(__name__)


@dataclass
class ArticleIngestionInput:
    user_id: str
    source_id: str
    source_type: str
    source_item_id: str
    item: RawItem
    title: str | None
    text: str
    raw_ref: dict
    time_payload: dict
    is_primary: bool | None = None
    parent_index_id: str | None = None
    analysis_text: str | None = None
    use_entity_context: bool = True
    wiki_source_type: str | None = None
    write_summary_wiki: bool = True


@dataclass
class ArticleIngestionResult:
    article_id: str
    summary_id: str
    abstract: str
    tags: list[str]
    entities: list[dict]
    promoted_entity_ids: list[str]


@dataclass
class ArticleIngestionAdapters:
    analyze_article: Callable[[str, list[dict], list[dict], list[dict]], dict]
    embed: Callable[[str], Awaitable[list[float]]]
    post_ingest: Callable[[dict], Awaitable[str]]
    get_analysis_context: Callable[[list[float]], Awaitable[dict]]
    process_entity_candidates: Callable[[str, list[dict]], Awaitable[dict]]
    fetch_node: Callable[[str], Awaitable[dict | None]]
    generate_entity_page: Callable[[str, list[str], list[str]], str]
    mark_candidate_promoted: Callable[[int, str], Awaitable[None]]
    backfill_wikilinks: Callable[[str], Awaitable[None]]
    write_wiki_article: Callable[[str, RawItem, str, list[str], dict, str | None, str | None], None]
    write_wiki_summary: Callable[[str, str, str, str, list[str], str], None]
    write_wiki_entity: Callable[[str, str, list[str], list[str], str, list[str]], None]
    max_entity_page_sources: int
    embedding_model: str   # 当前使用的 embedding model 名（用于写入 nodes.embedding_model）


async def process_article_like_item(
    data: ArticleIngestionInput,
    adapters: ArticleIngestionAdapters,
) -> ArticleIngestionResult:
    analysis_text = data.analysis_text or data.text
    initial_embedding = None
    nearby_entities: list[dict] = []
    top_candidates: list[dict] = []
    popular_tags: list[dict] = []

    if data.use_entity_context:
        initial_embedding = await adapters.embed(data.text[:8000])
        context = await adapters.get_analysis_context(initial_embedding)
        nearby_entities = context.get("nearby_entities", [])
        top_candidates = context.get("top_candidates", [])
        popular_tags = context.get("popular_tags", [])

    analysis = adapters.analyze_article(analysis_text, nearby_entities, top_candidates, popular_tags)
    abstract = analysis["abstract"]
    tags = analysis["tags"]
    entities = analysis["entities"]

    if abstract:
        embedding = await adapters.embed(abstract)
    elif initial_embedding is not None:
        embedding = initial_embedding
    else:
        embedding = await adapters.embed(analysis_text[:8000])

    article_payload = {
        "user_id": data.user_id,
        "title": data.title,
        "abstract": abstract,
        "embedding": embedding,
        "embedding_model": adapters.embedding_model,
        "source_type": data.source_type,
        "source_id": data.source_id,
        "raw_ref": data.raw_ref,
        "tags": tags,
        "object_type": "article",
        "source_item_id": data.source_item_id,
        # doc_kind 不在此显式提供——API 层 ingest() 会沿 source_items → sources → default cascade 自动填充
        **data.time_payload,
    }
    if data.is_primary is not None:
        article_payload["is_primary"] = data.is_primary
    if data.parent_index_id:
        article_payload["parent_index_id"] = data.parent_index_id

    article_id = await adapters.post_ingest(article_payload)
    summary_embedding = await adapters.embed(abstract) if abstract else embedding
    display_title = data.title or article_id
    summary_id = await adapters.post_ingest({
        "user_id": data.user_id,
        "title": f"摘要：{display_title}",
        "abstract": abstract,
        "embedding": summary_embedding,
        "embedding_model": adapters.embedding_model,
        "source_type": data.source_type,
        "source_id": data.source_id,
        "raw_ref": {},
        "tags": tags,
        "object_type": "summary",
        "summary_of": article_id,
        "source_node_ids": [article_id],
    })

    created = data.item.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    adapters.write_wiki_article(
        article_id,
        data.item,
        data.text,
        tags,
        data.raw_ref,
        display_title,
        data.wiki_source_type,
    )
    if data.write_summary_wiki:
        adapters.write_wiki_summary(summary_id, article_id, display_title, abstract, tags, created)

    promoted_entity_ids = await _promote_entities(data, adapters, article_id, entities)
    for entity_id in promoted_entity_ids:
        try:
            await adapters.backfill_wikilinks(entity_id)
        except Exception as exc:
            logger.warning("[%s] wikilink backfill failed for %s: %s", data.source_id, entity_id, exc)

    return ArticleIngestionResult(
        article_id=article_id,
        summary_id=summary_id,
        abstract=abstract,
        tags=tags,
        entities=entities,
        promoted_entity_ids=promoted_entity_ids,
    )


async def _promote_entities(
    data: ArticleIngestionInput,
    adapters: ArticleIngestionAdapters,
    article_id: str,
    entities: list[dict],
) -> list[str]:
    if not entities:
        return []

    promoted_entity_ids: list[str] = []
    candidate_result = await adapters.process_entity_candidates(article_id, entities)
    promoted_list = candidate_result.get("promoted", [])

    for promoted in promoted_list:
        try:
            source_abstracts = []
            source_article_ids = promoted.get("source_article_ids", [])
            for source_article_id in source_article_ids[: adapters.max_entity_page_sources]:
                article = await adapters.fetch_node(source_article_id)
                if article and article.get("abstract"):
                    title = article.get("title") or source_article_id
                    source_abstracts.append(f"《{title}》: {article['abstract']}")

            entity_body = adapters.generate_entity_page(
                promoted["canonical_name"],
                promoted.get("aliases", []),
                source_abstracts,
            )
            entity_embedding = await adapters.embed(promoted["canonical_name"])
            entity_id = await adapters.post_ingest({
                "user_id": data.user_id,
                "title": promoted["canonical_name"],
                "abstract": entity_body[:500],
                "embedding": entity_embedding,
                "embedding_model": adapters.embedding_model,
                "source_type": "entity",
                "source_id": data.source_id,
                "raw_ref": {},
                "tags": [],
                "object_type": "entity",
                "source_node_ids": source_article_ids,
                "canonical_name": promoted["canonical_name"],
                "aliases": promoted.get("aliases", []),
            })

            adapters.write_wiki_entity(
                entity_id,
                promoted["canonical_name"],
                promoted.get("aliases", []),
                source_article_ids,
                entity_body,
                [],
            )
            await adapters.mark_candidate_promoted(promoted["candidate_id"], entity_id)
            promoted_entity_ids.append(entity_id)
            logger.info("[%s] entity 晋升入库: %s — %s", data.source_id, entity_id, promoted["canonical_name"])
        except Exception as exc:
            logger.error(
                "[%s] entity 生成失败: %s — %s",
                data.source_id,
                promoted.get("canonical_name"),
                exc,
                exc_info=True,
            )

    return promoted_entity_ids

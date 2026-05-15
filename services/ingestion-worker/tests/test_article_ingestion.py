import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from article_ingestion import (  # noqa: E402
    ArticleIngestionAdapters,
    ArticleIngestionInput,
    process_article_like_item,
)
from sources.base import RawItem  # noqa: E402


class FakeIngestion:
    def __init__(self, *, abstract: str = "abstract", entities: list[dict] | None = None):
        self.abstract = abstract
        self.entities = entities if entities is not None else [{"name": "Entity"}]
        self.embed_calls: list[str] = []
        self.context_calls: list[list[float]] = []
        self.analyze_calls: list[tuple[str, list[dict], list[dict]]] = []
        self.posted: list[dict] = []
        self.wiki_articles: list[tuple] = []
        self.wiki_summaries: list[tuple] = []
        self.wiki_entities: list[tuple] = []
        self.marked: list[tuple[int, str]] = []
        self.backfilled: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return [float(len(text))]

    async def get_analysis_context(self, embedding: list[float]) -> dict:
        self.context_calls.append(embedding)
        return {
            "nearby_entities": [{"id": "ent_old", "title": "Old Entity"}],
            "top_candidates": [{"canonical_name": "Candidate", "mention_count": 2}],
        }

    def analyze_article(self, text: str, nearby_entities: list[dict], top_candidates: list[dict]) -> dict:
        self.analyze_calls.append((text, nearby_entities, top_candidates))
        return {"abstract": self.abstract, "tags": ["tag"], "entities": self.entities}

    async def post_ingest(self, payload: dict) -> str:
        self.posted.append(payload)
        object_type = payload["object_type"]
        return {"article": "art_1", "summary": "sum_1", "entity": "ent_1"}[object_type]

    async def process_entity_candidates(self, article_id: str, entities: list[dict]) -> dict:
        return {
            "promoted": [
                {
                    "candidate_id": 7,
                    "canonical_name": "Entity",
                    "aliases": ["Alias"],
                    "source_article_ids": [article_id],
                }
            ]
        }

    async def fetch_node(self, node_id: str) -> dict | None:
        return {"title": "Article", "abstract": "Article abstract"}

    def generate_entity_page(self, canonical_name: str, aliases: list[str], source_abstracts: list[str]) -> str:
        return f"{canonical_name}: {'; '.join(source_abstracts)}"

    async def mark_candidate_promoted(self, candidate_id: int, entity_node_id: str) -> None:
        self.marked.append((candidate_id, entity_node_id))

    async def backfill_wikilinks(self, entity_id: str) -> None:
        self.backfilled.append(entity_id)

    def write_wiki_article(
        self,
        node_id: str,
        item: RawItem,
        text: str,
        tags: list[str],
        raw_ref: dict,
        title_override: str | None,
        source_type_override: str | None,
    ) -> None:
        self.wiki_articles.append((node_id, text, tags, raw_ref, title_override, source_type_override))

    def write_wiki_summary(
        self,
        summary_id: str,
        article_id: str,
        article_title: str,
        abstract: str,
        tags: list[str],
        created: str,
    ) -> None:
        self.wiki_summaries.append((summary_id, article_id, article_title, abstract, tags, created))

    def write_wiki_entity(
        self,
        entity_id: str,
        canonical_name: str,
        aliases: list[str],
        source_ids: list[str],
        body: str,
        tags: list[str],
    ) -> None:
        self.wiki_entities.append((entity_id, canonical_name, aliases, source_ids, body, tags))

    def adapters(self) -> ArticleIngestionAdapters:
        return ArticleIngestionAdapters(
            analyze_article=self.analyze_article,
            embed=self.embed,
            post_ingest=self.post_ingest,
            get_analysis_context=self.get_analysis_context,
            process_entity_candidates=self.process_entity_candidates,
            fetch_node=self.fetch_node,
            generate_entity_page=self.generate_entity_page,
            mark_candidate_promoted=self.mark_candidate_promoted,
            backfill_wikilinks=self.backfill_wikilinks,
            write_wiki_article=self.write_wiki_article,
            write_wiki_summary=self.write_wiki_summary,
            write_wiki_entity=self.write_wiki_entity,
            max_entity_page_sources=5,
        )


def raw_item() -> RawItem:
    return RawItem(
        source_id="src_1",
        title="Title",
        raw_ref={"type": "url", "url": "https://example.com"},
        content_type="text/html",
        raw_bytes=None,
        fetched_at=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
    )


class ArticleIngestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_regular_article_flow_uses_entity_context_and_promotes_entities(self):
        fake = FakeIngestion()

        result = await process_article_like_item(
            ArticleIngestionInput(
                user_id="default",
                source_id="src_1",
                source_type="rss",
                source_item_id="si_1",
                item=raw_item(),
                title="Title",
                text="Article text",
                raw_ref={"type": "url", "url": "https://example.com", "cached": "/tmp/raw.html"},
                time_payload={"captured_at": "2026-05-15T10:00:00+00:00"},
                is_primary=True,
                use_entity_context=True,
            ),
            fake.adapters(),
        )

        self.assertEqual(result.article_id, "art_1")
        self.assertEqual(result.summary_id, "sum_1")
        self.assertEqual(result.promoted_entity_ids, ["ent_1"])
        self.assertEqual(len(fake.context_calls), 1)
        self.assertEqual(fake.analyze_calls[0][1], [{"id": "ent_old", "title": "Old Entity"}])
        self.assertEqual(fake.posted[0]["object_type"], "article")
        self.assertEqual(fake.posted[0]["is_primary"], True)
        self.assertEqual(fake.posted[0]["captured_at"], "2026-05-15T10:00:00+00:00")
        self.assertEqual(fake.posted[1]["summary_of"], "art_1")
        self.assertEqual(fake.posted[2]["object_type"], "entity")
        self.assertEqual(fake.wiki_articles[0][0], "art_1")
        self.assertEqual(fake.wiki_summaries[0][2], "Title")
        self.assertEqual(fake.marked, [(7, "ent_1")])
        self.assertEqual(fake.backfilled, ["ent_1"])

    async def test_book_chapter_flow_skips_entity_context_and_sets_parent_index(self):
        fake = FakeIngestion(entities=[])

        await process_article_like_item(
            ArticleIngestionInput(
                user_id="default",
                source_id="book_src",
                source_type="book",
                source_item_id="si_book",
                item=raw_item(),
                title="Chapter 1",
                text="Full chapter text",
                raw_ref={"type": "book_chapter", "path": "/tmp/book.epub::chapter::1"},
                time_payload={},
                parent_index_id="idx_1",
                analysis_text="Truncated chapter text",
                use_entity_context=False,
                wiki_source_type="book_chapter",
                write_summary_wiki=False,
            ),
            fake.adapters(),
        )

        self.assertEqual(fake.context_calls, [])
        self.assertEqual(fake.analyze_calls[0][0], "Truncated chapter text")
        self.assertEqual(fake.analyze_calls[0][1], [])
        self.assertEqual(fake.posted[0]["parent_index_id"], "idx_1")
        self.assertNotIn("is_primary", fake.posted[0])
        self.assertEqual(fake.posted[0]["raw_ref"]["type"], "book_chapter")
        self.assertEqual(fake.wiki_articles[0][5], "book_chapter")
        self.assertEqual(fake.wiki_summaries, [])


if __name__ == "__main__":
    unittest.main()

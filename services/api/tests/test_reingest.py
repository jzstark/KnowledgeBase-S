import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from kb import ingest


class _Transaction:
    def __init__(self):
        self.exception_type = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exception_type = exc_type
        return False


class _ReingestDatabase:
    def __init__(self):
        self.transaction_context = _Transaction()
        self.executed: list[tuple[str, dict | None]] = []

    def transaction(self):
        return self.transaction_context

    async def fetch_one(self, query, values):
        return {"id": "art_existing"}

    async def execute(self, query, values=None):
        self.executed.append((query, values))


class _ArticleReplaceDatabase:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    async def fetch_one(self, query, values):
        if "an.document_instance_id" in query:
            return {"id": "art_legacy_id"}
        raise AssertionError(query)

    async def execute(self, query, values):
        self.executed.append((query, values))


class _SummaryReplaceDatabase:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []
        self.queries: list[str] = []

    async def fetch_one(self, query, values):
        self.queries.append(query)
        if "JOIN summary_nodes" in query:
            return {"id": "sum_default_existing"}
        raise AssertionError(query)

    async def execute(self, query, values):
        self.executed.append((query, values))


def _request() -> ingest.ReingestRequest:
    common = {
        "user_id": "default",
        "abstract": "new abstract",
        "embedding": [0.1, 0.2],
        "source_type": "rss",
        "source_id": "src_real",
        "raw_ref": {},
        "tags": ["new"],
        "doc_kind": "analysis",
    }
    return ingest.ReingestRequest(
        article=ingest.IngestRequest(
            **common,
            title="New title",
            object_type="article",
            source_item_id="si_1",
            document_instance_id="di_1",
        ),
        summary=ingest.IngestRequest(
            **common,
            title="摘要：New title",
            object_type="summary",
        ),
        entities=[],
    )


class ReingestTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_ingest_still_deduplicates(self):
        fake = _ArticleReplaceDatabase()

        with patch.object(ingest.database, "database", fake):
            result = await ingest.do_ingest(_request().article)

        self.assertEqual(result, {"id": "art_legacy_id", "duplicate": True})
        self.assertEqual(fake.executed, [])

    async def test_replace_ingest_preserves_existing_article_id(self):
        fake = _ArticleReplaceDatabase()
        request = _request().article

        with patch.object(ingest.database, "database", fake):
            result = await ingest.do_ingest(request, replace_existing=True)

        self.assertEqual(result, "art_legacy_id")
        self.assertEqual(fake.executed[0][1]["id"], "art_legacy_id")
        self.assertIn("ON CONFLICT (id) DO UPDATE", fake.executed[0][0])

    async def test_replace_ingest_updates_existing_default_summary_id(self):
        fake = _SummaryReplaceDatabase()
        summary = _request().summary.copy(
            update={
                "summary_of": "art_existing",
                "source_node_ids": ["art_existing"],
                "perspective_embedding": [0.3, 0.4],
            }
        )
        embed_text = AsyncMock(return_value=[9.9, 9.9])

        with (
            patch.object(ingest.database, "database", fake),
            patch.object(ingest, "_embed_text", embed_text),
        ):
            result = await ingest.do_ingest(summary, replace_existing=True)

        self.assertEqual(result, "sum_default_existing")
        self.assertEqual(fake.executed[0][1]["id"], "sum_default_existing")
        self.assertTrue(any("sn.is_default = true" in query for query in fake.queries))
        embed_text.assert_not_awaited()

    async def test_replaces_article_and_default_summary_in_one_transaction(self):
        fake = _ReingestDatabase()
        do_ingest = AsyncMock(side_effect=["art_existing", "sum_existing"])

        with (
            patch.object(ingest.database, "database", fake),
            patch.object(ingest, "do_ingest", do_ingest),
            patch.object(ingest, "_reset_article_entity_derivatives", AsyncMock()),
            patch.object(
                ingest,
                "do_process_entity_candidates",
                AsyncMock(return_value={"matched_existing": [], "promoted": []}),
            ),
        ):
            result = await ingest.do_reingest(_request())

        self.assertEqual(result["article_id"], "art_existing")
        self.assertEqual(result["summary_id"], "sum_existing")
        self.assertTrue(do_ingest.await_args_list[0].kwargs["replace_existing"])
        summary_payload = do_ingest.await_args_list[1].args[0]
        self.assertEqual(summary_payload.summary_of, "art_existing")
        self.assertEqual(summary_payload.source_node_ids, ["art_existing"])
        self.assertIn("DELETE FROM knowledge_edges", fake.executed[0][0])
        self.assertIsNone(fake.transaction_context.exception_type)

    async def test_summary_failure_leaves_transaction_to_roll_back(self):
        fake = _ReingestDatabase()
        do_ingest = AsyncMock(side_effect=["art_existing", RuntimeError("summary write failed")])

        with (
            patch.object(ingest.database, "database", fake),
            patch.object(ingest, "do_ingest", do_ingest),
        ):
            with self.assertRaisesRegex(RuntimeError, "summary write failed"):
                await ingest.do_reingest(_request())

        self.assertIs(fake.transaction_context.exception_type, RuntimeError)


if __name__ == "__main__":
    unittest.main()

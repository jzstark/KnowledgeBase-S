import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")

import document_types


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DocumentTypeDatabase:
    def __init__(self, *, document_status: str = "succeeded", source_status: str = "succeeded"):
        self.document_status = document_status
        self.source_status = source_status
        self.executed: list[tuple[str, dict]] = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values):
        return {"id": values["id"], "folder_id": "fld_test", "status": self.document_status}

    async def fetch_all(self, query, values):
        if "FROM article_nodes" in query:
            return [{"node_id": "art_1"}, {"node_id": "art_2"}]
        if "FROM summary_nodes" in query:
            return [{"node_id": "sum_1"}, {"node_id": "sum_2"}]
        if "FROM source_items" in query:
            return [{"id": "si_1", "status": self.source_status}]
        raise AssertionError(query)

    async def execute(self, query, values):
        self.executed.append((query, values))


class _LegacySourceDatabase:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values):
        if "SELECT si.document_instance_id" in query:
            return {
                "document_instance_id": "di_test",
                "source_id": "src_test",
                "default_doc_kind": "analysis",
            }
        if "FROM source_items" in query:
            return {"id": "si_test", "status": "succeeded", "document_instance_id": "di_test"}
        if "FROM document_instances" in query:
            return {"status": "succeeded"}
        raise AssertionError(query)

    async def fetch_all(self, query, values):
        if "FROM article_nodes" in query:
            return [{"node_id": "art_1"}]
        if "FROM summary_nodes" in query:
            return [{"node_id": "sum_1"}]
        raise AssertionError(query)

    async def execute(self, query, values):
        self.executed.append((query, values))


class DocumentTypeSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_document_source_articles_and_summaries(self):
        fake = _DocumentTypeDatabase()

        with patch.object(document_types.database, "database", fake):
            with patch.object(document_types, "sync_wiki_doc_kind", return_value=[]):
                result = await document_types.set_document_instance_doc_kind(
                    "di_test", "news", folder_id="fld_test"
                )

        self.assertEqual(result.article_ids, ["art_1", "art_2"])
        self.assertEqual(result.summary_ids, ["sum_1", "sum_2"])
        self.assertEqual(len(fake.executed), 3)
        self.assertIn("UPDATE document_instances", fake.executed[0][0])
        self.assertIn("UPDATE source_items", fake.executed[1][0])
        self.assertIn("UPDATE knowledge_nodes", fake.executed[2][0])
        self.assertEqual(
            fake.executed[2][1]["node_ids"],
            ["art_1", "art_2", "sum_1", "sum_2"],
        )

    async def test_rejects_processing_source_item_without_writes(self):
        fake = _DocumentTypeDatabase(source_status="processing")

        with patch.object(document_types.database, "database", fake):
            with self.assertRaises(document_types.DocumentTypeError) as raised:
                await document_types.set_document_instance_doc_kind("di_test", "news")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(fake.executed, [])

    async def test_legacy_clear_updates_all_records_and_uses_source_default(self):
        fake = _LegacySourceDatabase()

        with patch.object(document_types.database, "database", fake):
            with patch.object(document_types, "sync_wiki_doc_kind", return_value=[]):
                await document_types.set_source_item_doc_kind("si_test", None)

        self.assertEqual(len(fake.executed), 3)
        self.assertIn("WHERE document_instance_id = :id", fake.executed[0][0])
        self.assertIn("UPDATE document_instances", fake.executed[1][0])
        self.assertEqual(fake.executed[2][1]["doc_kind"], "analysis")

    def test_rejects_invalid_explicit_type(self):
        configured = SimpleNamespace(doc_kind=SimpleNamespace(values=["news", "analysis"]))
        with patch.object(document_types, "settings", configured):
            with self.assertRaises(document_types.DocumentTypeError):
                document_types.validate_explicit_doc_kind("not-a-real-type")

    def test_wiki_frontmatter_sync_preserves_body(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "default" / "wiki" / "articles" / "art_1.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\nid: art_1\ntype: article\ndoc_kind: news\n---\n\n# 标题\n\n正文---仍保留\n",
                encoding="utf-8",
            )

            with patch.object(document_types, "USER_DATA_DIR", root):
                warnings = document_types.sync_wiki_doc_kind(["art_1"], [], "analysis")

            frontmatter, body = document_types.split_frontmatter(path.read_text(encoding="utf-8"))
            self.assertEqual(warnings, [])
            self.assertEqual(yaml.safe_load(frontmatter)["doc_kind"], "analysis")
            self.assertIn("正文---仍保留", body)


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from fastapi import HTTPException
from routers import folders
import document_lifecycle
from kb import entity_knowledge


class _Transaction:
    def __init__(self):
        self.exception_type = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exception_type = exc_type
        return False


class _DeleteDatabase:
    def __init__(self, raw_path: str, *, status="succeeded", shared=False, error=None):
        self.raw_path = raw_path
        self.status = status
        self.shared = shared
        self.error = error
        self.transaction_context = _Transaction()
        self.executed: list[tuple[str, dict | None]] = []

    def transaction(self):
        return self.transaction_context

    async def fetch_one(self, query, values):
        if "FROM document_instances" in query and "FOR UPDATE" in query:
            return {"id": values["id"], "folder_id": "fld_1", "status": self.status}
        raise AssertionError(query)

    async def fetch_all(self, query, values):
        if "SELECT ra.storage_key, si.raw_snapshot_ref" in query:
            return [{
                "storage_key": self.raw_path,
                "raw_snapshot_ref": self.raw_path,
                "extracted_text_ref": None,
            }]
        if "SELECT ra.storage_key AS ref" in query:
            return [{"ref": self.raw_path}] if self.shared else []
        if "SELECT id, status" in query and "FROM source_items" in query:
            return [{"id": "si_1", "status": self.status, "error": self.error}]
        if "SELECT DISTINCT an.node_id" in query:
            return [{"node_id": "art_1"}]
        if "SELECT node_id FROM summary_nodes" in query:
            return [{"node_id": "sum_default"}, {"node_id": "sum_custom"}]
        if "SELECT DISTINCT entity_id" in query:
            return [{"entity_id": "ent_1"}]
        raise AssertionError(query)

    async def execute(self, query, values=None):
        self.executed.append((query, values))


class _FolderDatabase:
    async def fetch_one(self, query, values):
        return {"id": values["id"]}


class DocumentDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.remove_article = AsyncMock(return_value=["ent_1"])
        patcher = patch.object(entity_knowledge, "remove_article", self.remove_article)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_preview_excludes_blocked_documents_from_delete_totals(self):
        eligible = {
            "id": "di_1", "name": "one", "status": "eligible", "detail": "可永久删除",
            "articles": 1, "summaries": 2, "removable_files": 0, "shared_files": 0,
        }
        blocked = {
            "id": "di_2", "name": "two", "status": "blocked", "detail": "正在处理",
            "articles": 3, "summaries": 4, "removable_files": 0, "shared_files": 0,
        }
        path = Path("/tmp/phase6-preview-file")

        async def file_paths(ids):
            return {path} if "di_1" in ids else set()

        with (
            patch.object(document_lifecycle, "_delete_impact", AsyncMock(side_effect=[eligible, blocked])),
            patch.object(document_lifecycle, "_document_file_paths", side_effect=file_paths),
            patch.object(document_lifecycle, "_shared_file_paths", AsyncMock(return_value=set())),
        ):
            preview = await document_lifecycle._build_delete_preview("fld_1", ["di_1", "di_2"])

        self.assertEqual(preview["eligible"], 1)
        self.assertEqual(preview["blocked"], 1)
        self.assertEqual(preview["articles"], 1)
        self.assertEqual(preview["summaries"], 2)
        self.assertEqual(preview["removable_files"], 1)

    async def test_hard_delete_removes_articles_summaries_and_tombstones(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            raw = base / "raw.txt"
            raw.write_text("raw", encoding="utf-8")
            article_wiki = base / "article_art_1.md"
            summary_default = base / "summary_sum_default.md"
            summary_custom = base / "summary_sum_custom.md"
            for path in (article_wiki, summary_default, summary_custom):
                path.write_text("wiki", encoding="utf-8")
            fake = _DeleteDatabase(str(raw))

            def wiki_path(_user_id, node_id, object_type):
                return base / f"{object_type}_{node_id}.md"

            with (
                patch.object(folders.database, "database", fake),
                patch.object(document_lifecycle, "USER_DATA_DIR", base),
                patch.object(document_lifecycle, "wiki_file_path", wiki_path),
            ):
                result = await document_lifecycle._hard_delete_document_instance(
                    "di_1", folder_id="fld_1"
                )
            self.assertFalse(raw.exists())
            self.assertFalse(article_wiki.exists())
            self.assertFalse(summary_default.exists())
            self.assertFalse(summary_custom.exists())

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["articles"], 1)
        self.assertEqual(result["summaries"], 2)
        self.remove_article.assert_awaited_once_with("art_1", user_id="default")
        sql = "\n".join(query for query, _ in fake.executed)
        self.assertIn("DELETE FROM knowledge_nodes", sql)
        self.assertIn("UPDATE source_items", sql)
        self.assertIn("status = 'deleted'", sql)
        self.assertIn("UPDATE document_instances SET status = 'deleted'", sql)
        self.assertIsNone(fake.transaction_context.exception_type)

    async def test_shared_raw_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            raw = base / "shared.txt"
            raw.write_text("shared", encoding="utf-8")
            fake = _DeleteDatabase(str(raw), shared=True)

            with (
                patch.object(folders.database, "database", fake),
                patch.object(document_lifecycle, "USER_DATA_DIR", base),
                patch.object(
                    document_lifecycle,
                    "wiki_file_path",
                    lambda *_: base / "missing-wiki.md",
                ),
            ):
                result = await document_lifecycle._hard_delete_document_instance("di_1")

            self.assertTrue(raw.exists())
            self.assertEqual(result["shared_files_preserved"], 1)

    async def test_processing_document_is_rejected_before_database_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            raw = base / "processing.txt"
            raw.write_text("raw", encoding="utf-8")
            fake = _DeleteDatabase(str(raw), status="processing")

            with (
                patch.object(folders.database, "database", fake),
                patch.object(document_lifecycle, "USER_DATA_DIR", base),
            ):
                result = await document_lifecycle._hard_delete_document_instance("di_1")
            self.assertTrue(raw.exists())

        self.assertEqual(result["status"], "failed")
        self.assertIn("正在处理", result["detail"])
        self.assertEqual(fake.executed, [])

    async def test_file_cleanup_failure_is_reported_after_database_commit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            raw = base / "locked.txt"
            raw.write_text("raw", encoding="utf-8")
            fake = _DeleteDatabase(str(raw))

            with (
                patch.object(folders.database, "database", fake),
                patch.object(document_lifecycle, "USER_DATA_DIR", base),
                patch.object(
                    document_lifecycle,
                    "wiki_file_path",
                    lambda *_: base / "missing-wiki.md",
                ),
                patch.object(Path, "unlink", side_effect=PermissionError("permission denied")),
            ):
                result = await document_lifecycle._hard_delete_document_instance("di_1")

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(len(result["file_warnings"]), 1)
        self.assertIn("部分文件清理失败", result["detail"])

    async def test_repeated_delete_retries_pending_file_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            leftover = base / "leftover.md"
            leftover.write_text("wiki", encoding="utf-8")
            error = "file_cleanup_pending:" + f'["{leftover}"]'
            fake = _DeleteDatabase(
                str(base / "missing-raw.txt"), status="deleted", error=error
            )

            with (
                patch.object(folders.database, "database", fake),
                patch.object(document_lifecycle, "USER_DATA_DIR", base),
            ):
                result = await document_lifecycle._hard_delete_document_instance("di_1")

            self.assertFalse(leftover.exists())

        self.assertEqual(result["status"], "skipped")
        self.assertIn("遗留文件清理完成", result["detail"])
        self.assertEqual(result["file_warnings"], [])

    async def test_changed_preview_requires_confirmation_again(self):
        preview = {
            "confirmation_token": "new-token",
            "documents": 1,
            "eligible": 0,
            "blocked": 1,
            "skipped": 0,
            "failed": 0,
            "articles": 1,
            "summaries": 1,
            "removable_files": 0,
            "shared_files": 1,
            "results": [],
        }
        with (
            patch.object(folders.database, "database", _FolderDatabase()),
            patch.object(document_lifecycle, "_build_delete_preview", AsyncMock(return_value=preview)),
        ):
            with self.assertRaises(HTTPException) as raised:
                await folders.delete_document_instances(
                    folders.DocumentInstanceDeleteBatchRequest(
                        folder_id="fld_1",
                        ids=["di_1"],
                        confirmation_token="old-token",
                    ),
                    _={},
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "impact_changed")
        self.assertEqual(raised.exception.detail["preview"], preview)

    async def test_batch_reports_partial_database_failure(self):
        preview = {"confirmation_token": "token"}
        delete_one = AsyncMock(
            side_effect=[
                {
                    "id": "di_1", "status": "deleted", "detail": "永久删除成功",
                    "file_warnings": [],
                },
                RuntimeError("database unavailable"),
            ]
        )
        with (
            patch.object(folders.database, "database", _FolderDatabase()),
            patch.object(document_lifecycle, "_build_delete_preview", AsyncMock(return_value=preview)),
            patch.object(document_lifecycle, "_hard_delete_document_instance", delete_one),
            patch.object(folders.logger, "exception"),
        ):
            result = await folders.delete_document_instances(
                folders.DocumentInstanceDeleteBatchRequest(
                    folder_id="fld_1", ids=["di_1", "di_2"], confirmation_token="token"
                ),
                _={},
            )

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["results"][1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

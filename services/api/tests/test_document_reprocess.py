import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from fastapi import HTTPException
from routers import folders, sources


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ReprocessDatabase:
    def __init__(
        self, *, document_status="succeeded", item_status="succeeded", article_count=1
    ):
        self.document_status = document_status
        self.item_status = item_status
        self.article_count = article_count
        self.executed: list[tuple[str, dict]] = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values):
        return {"id": "di_1", "status": self.document_status}

    async def fetch_all(self, query, values):
        if "FROM source_items si" in query:
            return [{
                "id": "si_1",
                "status": self.item_status,
                "source_id": "src_actual",
                "source_deleted_at": None,
                "raw_snapshot_ref": None,
                "origin_ref": "https://example.com/article",
                "origin_ref_type": "url",
                "reprocess_requested_at": None,
            }]
        if "FROM article_nodes an" in query:
            return [
                {"node_id": f"art_{index}", "source_item_id": "si_1"}
                for index in range(self.article_count)
            ]
        raise AssertionError(query)

    async def execute(self, query, values):
        self.executed.append((query, values))


class _Response:
    def raise_for_status(self):
        return None


class _Client:
    posted_url = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, url, timeout):
        type(self).posted_url = url
        return _Response()


class _StatusDatabase:
    def __init__(self):
        self.query = ""

    async def fetch_one(self, query, values):
        self.query = query
        return {"id": values["id"], "document_instance_id": None}


class _FolderDatabase:
    async def fetch_one(self, query, values):
        return {"id": values["id"]}


class DocumentReprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_status_clears_consumed_reprocess_intent(self):
        fake = _StatusDatabase()

        with patch.object(sources.database, "database", fake):
            await sources.update_source_item_status(
                "si_1",
                sources.SourceItemStatusUpdate(status="succeeded"),
                _={"sub": "service"},
            )

        self.assertIn("reprocess_requested_at = NULL", fake.query)

    async def test_queues_persistent_regeneration_and_uses_real_source(self):
        fake = _ReprocessDatabase()

        with (
            patch.object(folders.database, "database", fake),
            patch.object(folders.httpx, "AsyncClient", _Client),
        ):
            result = await folders.reprocess_document_instance("di_1", _={})

        self.assertEqual(result["mode"], "regenerate")
        self.assertEqual(result["source_id"], "src_actual")
        self.assertTrue(_Client.posted_url.endswith("/trigger/src_actual"))
        self.assertIn("reprocess_requested_at", fake.executed[0][0])
        self.assertEqual(len(fake.executed), 2)

    async def test_rejects_processing_document_without_writes(self):
        fake = _ReprocessDatabase(document_status="processing")

        with patch.object(folders.database, "database", fake):
            with self.assertRaises(HTTPException) as raised:
                await folders.reprocess_document_instance("di_1", _={})

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(fake.executed, [])

    async def test_rejects_already_pending_document_without_writes(self):
        fake = _ReprocessDatabase(document_status="pending")

        with patch.object(folders.database, "database", fake):
            with self.assertRaises(HTTPException) as raised:
                await folders.reprocess_document_instance("di_1", _={})

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("已经排队", raised.exception.detail)
        self.assertEqual(fake.executed, [])

    async def test_rejects_multi_article_document_instead_of_updating_one(self):
        fake = _ReprocessDatabase(article_count=2)

        with patch.object(folders.database, "database", fake):
            with self.assertRaises(HTTPException) as raised:
                await folders.reprocess_document_instance("di_1", _={})

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("关联多篇文章", raised.exception.detail)
        self.assertEqual(fake.executed, [])

    async def test_batch_deduplicates_ids_and_triggers_each_source_once(self):
        queue = AsyncMock(
            side_effect=[
                {
                    "id": "di_1",
                    "status": "accepted",
                    "detail": "已排队",
                    "source_id": "src_shared",
                },
                {
                    "id": "di_2",
                    "status": "accepted",
                    "detail": "已排队",
                    "source_id": "src_shared",
                },
                {
                    "id": "di_3",
                    "status": "skipped",
                    "detail": "文档正在处理，已跳过",
                    "http_status": 409,
                },
            ]
        )
        trigger = AsyncMock(return_value={"src_shared": False})

        with (
            patch.object(folders.database, "database", _FolderDatabase()),
            patch.object(folders, "_queue_document_instance_reprocess", queue),
            patch.object(folders, "_trigger_reprocess_sources", trigger),
        ):
            result = await folders.reprocess_document_instances(
                folders.DocumentInstanceBatchRequest(
                    folder_id="fld_1", ids=["di_1", "di_1", "di_2", "di_3"]
                ),
                _={},
            )

        self.assertEqual(queue.await_count, 3)
        self.assertEqual(queue.await_args_list[0].kwargs["folder_id"], "fld_1")
        trigger.assert_awaited_once_with({"src_shared"})
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["deferred_sources"], 1)
        self.assertFalse(result["results"][0]["trigger_reached"])
        self.assertFalse(result["results"][1]["trigger_reached"])
        self.assertNotIn("http_status", result["results"][2])

    async def test_batch_reports_one_queue_failure_and_continues(self):
        queue = AsyncMock(
            side_effect=[
                RuntimeError("database unavailable"),
                {
                    "id": "di_2",
                    "status": "accepted",
                    "detail": "已排队",
                    "source_id": "src_2",
                },
            ]
        )
        trigger = AsyncMock(return_value={"src_2": True})

        with (
            patch.object(folders.database, "database", _FolderDatabase()),
            patch.object(folders, "_queue_document_instance_reprocess", queue),
            patch.object(folders, "_trigger_reprocess_sources", trigger),
            patch.object(folders.logger, "exception"),
        ):
            result = await folders.reprocess_document_instances(
                folders.DocumentInstanceBatchRequest(
                    folder_id="fld_1", ids=["di_1", "di_2"]
                ),
                _={},
            )

        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertTrue(result["results"][1]["trigger_reached"])


if __name__ == "__main__":
    unittest.main()

"""Lifecycle behavior against an isolated PostgreSQL database."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not os.environ.get("TEST_DATABASE_URL"):
    raise unittest.SkipTest("Run with scripts/test_document_lifecycle.sh")

from fastapi import HTTPException
import document_lifecycle
import document_types
from database import database
from routers import folders, sources


class DocumentLifecycleDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.connect()
        suffix = uuid4().hex[:12]
        self.source_id = f"src_test_{suffix}"
        self.folder_id = f"fld_test_{suffix}"
        self.document_id = f"di_test_{suffix}"
        self.item_id = f"si_test_{suffix}"
        await database.execute(
            "INSERT INTO sources (id, user_id, name, type) VALUES (:id, 'default', 'test', 'url')",
            {"id": self.source_id},
        )
        await database.execute(
            "INSERT INTO folders (id, user_id, name) VALUES (:id, 'default', 'test')",
            {"id": self.folder_id},
        )
        await database.execute(
            "INSERT INTO document_instances (id, user_id, folder_id, status) "
            "VALUES (:id, 'default', :folder, 'pending')",
            {"id": self.document_id, "folder": self.folder_id},
        )
        await database.execute(
            "INSERT INTO source_items "
            "(id, user_id, source_id, source_type, origin_ref, origin_ref_type, document_instance_id, status) "
            "VALUES (:id, 'default', :source, 'url', :origin, 'url', :document, 'pending')",
            {"id": self.item_id, "source": self.source_id,
             "origin": f"https://example.test/{suffix}", "document": self.document_id},
        )

    async def asyncTearDown(self):
        await database.execute("DROP TRIGGER IF EXISTS test_reject_document_update ON document_instances")
        await database.execute("DROP FUNCTION IF EXISTS test_reject_document_update()")
        await database.execute("DELETE FROM source_items WHERE id = :id", {"id": self.item_id})
        await database.execute("DELETE FROM document_instances WHERE id = :id", {"id": self.document_id})
        await database.execute("DELETE FROM folders WHERE id = :id", {"id": self.folder_id})
        await database.execute("DELETE FROM sources WHERE id = :id", {"id": self.source_id})
        await database.disconnect()

    async def test_worker_report_rolls_back_if_document_update_fails(self):
        await database.execute(
            "CREATE FUNCTION test_reject_document_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected document failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_document_update BEFORE UPDATE ON document_instances "
            f"FOR EACH ROW WHEN (NEW.id = '{self.document_id}') "
            "EXECUTE FUNCTION test_reject_document_update()"
        )

        with self.assertRaises(Exception):
            await sources.update_source_item_status(
                self.item_id, sources.SourceItemStatusUpdate(status="succeeded"), _={}
            )

        item_status = await database.fetch_val(
            "SELECT status FROM source_items WHERE id = :id", {"id": self.item_id}
        )
        document_status = await database.fetch_val(
            "SELECT status FROM document_instances WHERE id = :id", {"id": self.document_id}
        )
        self.assertEqual((item_status, document_status), ("pending", "pending"))

    async def test_retry_rolls_back_if_document_update_fails(self):
        await database.execute(
            "UPDATE source_items SET status = 'failed' WHERE id = :id", {"id": self.item_id}
        )
        await database.execute(
            "UPDATE document_instances SET status = 'failed' WHERE id = :id",
            {"id": self.document_id},
        )
        await database.execute(
            "CREATE FUNCTION test_reject_document_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected document failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_document_update BEFORE UPDATE ON document_instances "
            f"FOR EACH ROW WHEN (NEW.id = '{self.document_id}') "
            "EXECUTE FUNCTION test_reject_document_update()"
        )

        with self.assertRaises(Exception):
            await sources.retry_source_item(self.item_id, _={})

        item_status = await database.fetch_val(
            "SELECT status FROM source_items WHERE id = :id", {"id": self.item_id}
        )
        document_status = await database.fetch_val(
            "SELECT status FROM document_instances WHERE id = :id", {"id": self.document_id}
        )
        self.assertEqual((item_status, document_status), ("failed", "failed"))

    async def test_archived_item_rejects_worker_failure(self):
        await database.execute(
            "UPDATE source_items SET status = 'ignored' WHERE id = :id", {"id": self.item_id}
        )
        await database.execute(
            "UPDATE document_instances SET status = 'ignored' WHERE id = :id",
            {"id": self.document_id},
        )

        with self.assertRaises(HTTPException) as raised:
            await sources.update_source_item_status(
                self.item_id, sources.SourceItemStatusUpdate(status="failed"), _={}
            )

        self.assertEqual(raised.exception.status_code, 409)
        item_status = await database.fetch_val(
            "SELECT status FROM source_items WHERE id = :id", {"id": self.item_id}
        )
        self.assertEqual(item_status, "ignored")

    async def test_old_worker_cannot_claim_regeneration(self):
        await database.execute(
            "UPDATE source_items SET reprocess_requested_at = NOW() WHERE id = :id",
            {"id": self.item_id},
        )

        with self.assertRaises(HTTPException) as raised:
            await sources.update_source_item_status(
                self.item_id, sources.SourceItemStatusUpdate(status="processing"), _={}
            )

        self.assertEqual(raised.exception.status_code, 409)
        item_status = await database.fetch_val(
            "SELECT status FROM source_items WHERE id = :id", {"id": self.item_id}
        )
        self.assertEqual(item_status, "pending")

    async def test_success_consumes_regeneration_intent(self):
        await database.execute(
            "UPDATE source_items SET reprocess_requested_at = NOW() WHERE id = :id",
            {"id": self.item_id},
        )

        await sources.update_source_item_status(
            self.item_id,
            sources.SourceItemStatusUpdate(status="succeeded", reprocess_capable=True),
            _={},
        )

        intent = await database.fetch_val(
            "SELECT reprocess_requested_at FROM source_items WHERE id = :id",
            {"id": self.item_id},
        )
        document_status = await database.fetch_val(
            "SELECT status FROM document_instances WHERE id = :id", {"id": self.document_id}
        )
        self.assertIsNone(intent)
        self.assertEqual(document_status, "succeeded")

    async def test_batch_archive_continues_after_one_database_failure(self):
        other_ids = [f"di_test_{uuid4().hex[:12]}" for _ in range(2)]
        for document_id in other_ids:
            await database.execute(
                "INSERT INTO document_instances (id, user_id, folder_id, status) "
                "VALUES (:id, 'default', :folder, 'pending')",
                {"id": document_id, "folder": self.folder_id},
            )
        try:
            await database.execute(
                "CREATE FUNCTION test_reject_document_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'injected document failure'; END $$"
            )
            await database.execute(
                "CREATE TRIGGER test_reject_document_update BEFORE UPDATE ON document_instances "
                f"FOR EACH ROW WHEN (NEW.id = '{other_ids[0]}') "
                "EXECUTE FUNCTION test_reject_document_update()"
            )

            result = await folders.archive_document_instances(
                folders.DocumentInstanceBatchRequest(
                    folder_id=self.folder_id,
                    ids=[self.document_id, other_ids[0], other_ids[1]],
                ),
                _={},
            )

            self.assertEqual([item["status"] for item in result["results"]], ["archived", "failed", "archived"])
            statuses = [
                await database.fetch_val(
                    "SELECT status FROM document_instances WHERE id = :id", {"id": document_id}
                )
                for document_id in [self.document_id, *other_ids]
            ]
            self.assertEqual(statuses, ["ignored", "pending", "ignored"])
        finally:
            await database.execute("DROP TRIGGER IF EXISTS test_reject_document_update ON document_instances")
            await database.execute("DROP FUNCTION IF EXISTS test_reject_document_update()")
            for document_id in other_ids:
                await database.execute(
                    "DELETE FROM document_instances WHERE id = :id", {"id": document_id}
                )

    async def test_regeneration_batch_persists_each_request_before_one_source_trigger(self):
        second_document_id = f"di_test_{uuid4().hex[:12]}"
        second_item_id = f"si_test_{uuid4().hex[:12]}"
        await database.execute(
            "UPDATE source_items SET status = 'failed' WHERE id = :id", {"id": self.item_id}
        )
        await database.execute(
            "UPDATE document_instances SET status = 'failed' WHERE id = :id",
            {"id": self.document_id},
        )
        await database.execute(
            "INSERT INTO document_instances (id, user_id, folder_id, status) "
            "VALUES (:id, 'default', :folder, 'failed')",
            {"id": second_document_id, "folder": self.folder_id},
        )
        await database.execute(
            "INSERT INTO source_items "
            "(id, user_id, source_id, source_type, origin_ref, origin_ref_type, document_instance_id, status) "
            "VALUES (:id, 'default', :source, 'url', :origin, 'url', :document, 'failed')",
            {"id": second_item_id, "source": self.source_id,
             "origin": f"https://example.test/{second_item_id}", "document": second_document_id},
        )
        triggers = []

        async def trigger_sources(source_ids):
            statuses = await database.fetch_all(
                "SELECT status FROM source_items WHERE id = :first OR id = :second ORDER BY id",
                {"first": self.item_id, "second": second_item_id},
            )
            self.assertEqual([row["status"] for row in statuses], ["pending", "pending"])
            triggers.append(source_ids)
            return {self.source_id: False}

        try:
            result = await document_lifecycle.reprocess_documents(
                [self.document_id, second_document_id], self.folder_id,
                trigger_sources=trigger_sources,
            )
            self.assertEqual(result["accepted"], 2)
            self.assertEqual(triggers, [{self.source_id}])
            self.assertEqual(result["deferred_sources"], 1)
        finally:
            await database.execute("DELETE FROM source_items WHERE id = :id", {"id": second_item_id})
            await database.execute(
                "DELETE FROM document_instances WHERE id = :id", {"id": second_document_id}
            )

    async def test_worker_claim_and_archive_commit_one_consistent_state(self):
        claim, archive = await asyncio.gather(
            sources.update_source_item_status(
                self.item_id, sources.SourceItemStatusUpdate(status="processing"), _={}
            ),
            folders.delete_document_instance(self.document_id, hard=False, _={}),
            return_exceptions=True,
        )

        self.assertEqual(sum(isinstance(result, HTTPException) for result in (claim, archive)), 1)
        item_status = await database.fetch_val(
            "SELECT status FROM source_items WHERE id = :id", {"id": self.item_id}
        )
        document_status = await database.fetch_val(
            "SELECT status FROM document_instances WHERE id = :id", {"id": self.document_id}
        )
        self.assertEqual(item_status, document_status)
        self.assertIn(item_status, {"processing", "ignored"})

    async def test_only_one_worker_can_claim_a_document(self):
        claims = await asyncio.gather(
            sources.update_source_item_status(
                self.item_id, sources.SourceItemStatusUpdate(status="processing"), _={}
            ),
            sources.update_source_item_status(
                self.item_id, sources.SourceItemStatusUpdate(status="processing"), _={}
            ),
            return_exceptions=True,
        )

        self.assertEqual(sum(isinstance(result, HTTPException) for result in claims), 1)
        attempts = await database.fetch_val(
            "SELECT attempts FROM source_items WHERE id = :id", {"id": self.item_id}
        )
        self.assertEqual(attempts, 1)

    async def test_delete_requires_new_confirmation_after_impact_changes(self):
        request = folders.DocumentInstanceBatchRequest(
            folder_id=self.folder_id, ids=[self.document_id]
        )
        preview = await folders.preview_delete_document_instances(request, _={})
        await database.execute(
            "UPDATE document_instances SET status = 'processing' WHERE id = :id",
            {"id": self.document_id},
        )

        with self.assertRaises(HTTPException) as raised:
            await folders.delete_document_instances(
                folders.DocumentInstanceDeleteBatchRequest(
                    folder_id=self.folder_id,
                    ids=[self.document_id],
                    confirmation_token=preview["confirmation_token"],
                ),
                _={},
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "impact_changed")

    async def test_delete_keeps_file_on_rollback_then_cleans_it_after_commit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "raw.txt"
            raw_path.write_text("source material", encoding="utf-8")
            asset_id = f"ra_test_{uuid4().hex[:12]}"
            await database.execute(
                "INSERT INTO raw_assets (id, user_id, storage_key) VALUES (:id, 'default', :path)",
                {"id": asset_id, "path": str(raw_path)},
            )
            await database.execute(
                "UPDATE document_instances SET raw_asset_id = :asset WHERE id = :id",
                {"asset": asset_id, "id": self.document_id},
            )
            try:
                with patch.object(document_lifecycle, "USER_DATA_DIR", Path(temp_dir)):
                    preview = await document_lifecycle.preview_delete_documents(
                        [self.document_id], self.folder_id
                    )
                    await database.execute(
                        "CREATE FUNCTION test_reject_document_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
                        "BEGIN RAISE EXCEPTION 'injected document failure'; END $$"
                    )
                    await database.execute(
                        "CREATE TRIGGER test_reject_document_update BEFORE UPDATE ON document_instances "
                        f"FOR EACH ROW WHEN (NEW.id = '{self.document_id}') "
                        "EXECUTE FUNCTION test_reject_document_update()"
                    )

                    failed = await document_lifecycle.delete_documents(
                        [self.document_id], self.folder_id, preview["confirmation_token"]
                    )
                    self.assertEqual(failed["failed"], 1)
                    self.assertTrue(raw_path.exists())
                    await database.execute(
                        "DROP TRIGGER test_reject_document_update ON document_instances"
                    )
                    await database.execute("DROP FUNCTION test_reject_document_update()")

                    succeeded = await document_lifecycle.delete_documents(
                        [self.document_id], self.folder_id, preview["confirmation_token"]
                    )
                    self.assertEqual(succeeded["deleted"], 1)
                    self.assertFalse(raw_path.exists())
                    statuses = await database.fetch_one(
                        "SELECT si.status AS item_status, di.status AS document_status "
                        "FROM source_items si JOIN document_instances di ON di.id = si.document_instance_id "
                        "WHERE si.id = :id",
                        {"id": self.item_id},
                    )
                    self.assertEqual(
                        (statuses["item_status"], statuses["document_status"]),
                        ("deleted", "deleted"),
                    )
            finally:
                await database.execute(
                    "UPDATE document_instances SET raw_asset_id = NULL WHERE id = :id",
                    {"id": self.document_id},
                )
                await database.execute("DELETE FROM raw_assets WHERE id = :id", {"id": asset_id})

    async def test_type_change_and_worker_claim_finish_without_deadlock(self):
        results = await asyncio.wait_for(
            asyncio.gather(
                document_types.set_source_item_doc_kind(self.item_id, None),
                sources.update_source_item_status(
                    self.item_id, sources.SourceItemStatusUpdate(status="processing"), _={}
                ),
                return_exceptions=True,
            ),
            timeout=5,
        )
        for result in results:
            if isinstance(result, Exception):
                self.assertIsInstance(result, document_types.DocumentTypeError)
                self.assertEqual(result.status_code, 409)

    async def test_delete_preserves_file_shared_with_another_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "shared.txt"
            raw_path.write_text("shared source material", encoding="utf-8")
            asset_id = f"ra_test_{uuid4().hex[:12]}"
            other_document_id = f"di_test_{uuid4().hex[:12]}"
            await database.execute(
                "INSERT INTO raw_assets (id, user_id, storage_key) VALUES (:id, 'default', :path)",
                {"id": asset_id, "path": str(raw_path)},
            )
            await database.execute(
                "UPDATE document_instances SET raw_asset_id = :asset WHERE id = :id",
                {"asset": asset_id, "id": self.document_id},
            )
            await database.execute(
                "INSERT INTO document_instances (id, user_id, folder_id, raw_asset_id) "
                "VALUES (:id, 'default', :folder, :asset)",
                {"id": other_document_id, "folder": self.folder_id, "asset": asset_id},
            )
            try:
                with patch.object(document_lifecycle, "USER_DATA_DIR", Path(temp_dir)):
                    preview = await document_lifecycle.preview_delete_documents(
                        [self.document_id], self.folder_id
                    )
                    result = await document_lifecycle.delete_documents(
                        [self.document_id], self.folder_id, preview["confirmation_token"]
                    )
                self.assertEqual(result["deleted"], 1)
                self.assertEqual(result["results"][0]["shared_files_preserved"], 1)
                self.assertTrue(raw_path.exists())
            finally:
                await database.execute(
                    "UPDATE document_instances SET raw_asset_id = NULL "
                    "WHERE id = :first OR id = :second",
                    {"first": self.document_id, "second": other_document_id},
                )
                await database.execute(
                    "DELETE FROM document_instances WHERE id = :id", {"id": other_document_id}
                )
                await database.execute("DELETE FROM raw_assets WHERE id = :id", {"id": asset_id})

    async def test_file_cleanup_failure_can_be_retried_after_commit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "retry.txt"
            raw_path.write_text("source material", encoding="utf-8")
            asset_id = f"ra_test_{uuid4().hex[:12]}"
            await database.execute(
                "INSERT INTO raw_assets (id, user_id, storage_key) VALUES (:id, 'default', :path)",
                {"id": asset_id, "path": str(raw_path)},
            )
            await database.execute(
                "UPDATE document_instances SET raw_asset_id = :asset WHERE id = :id",
                {"asset": asset_id, "id": self.document_id},
            )
            try:
                with patch.object(document_lifecycle, "USER_DATA_DIR", Path(temp_dir)):
                    preview = await document_lifecycle.preview_delete_documents(
                        [self.document_id], self.folder_id
                    )
                    with patch.object(Path, "unlink", side_effect=PermissionError("injected failure")):
                        result = await document_lifecycle.delete_documents(
                            [self.document_id], self.folder_id, preview["confirmation_token"]
                        )
                    self.assertEqual(result["deleted"], 1)
                    self.assertTrue(result["file_warnings"])
                    self.assertTrue(raw_path.exists())
                    pending = await database.fetch_val(
                        "SELECT error FROM source_items WHERE id = :id", {"id": self.item_id}
                    )
                    self.assertTrue(pending.startswith("file_cleanup_pending:"))

                    retried = await document_lifecycle.delete_document(self.document_id)
                    self.assertEqual(retried["status"], "skipped")
                    self.assertFalse(raw_path.exists())
            finally:
                await database.execute(
                    "UPDATE document_instances SET raw_asset_id = NULL WHERE id = :id",
                    {"id": self.document_id},
                )
                await database.execute("DELETE FROM raw_assets WHERE id = :id", {"id": asset_id})

"""Document intake behavior against an isolated PostgreSQL database."""

import asyncio
import io
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not os.environ.get("TEST_DATABASE_URL"):
    raise unittest.SkipTest("Run with scripts/test_document_lifecycle.sh")

from fastapi import HTTPException, UploadFile

import document_intake
import document_lifecycle
from database import database
from routers import folders, sources


class DocumentIntakeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.connect()
        suffix = uuid4().hex[:12]
        self.folder_id = f"fld_{suffix}"
        self.source_id = f"src_{suffix}"
        self.storage = tempfile.TemporaryDirectory()
        await database.execute(
            "INSERT INTO sources (id, user_id, name, type) "
            "VALUES (:id, 'default', 'test', 'plaintext')",
            {"id": self.source_id},
        )
        await database.execute(
            "INSERT INTO folders (id, user_id, name) "
            "VALUES (:id, 'default', 'test')",
            {"id": self.folder_id},
        )

    async def asyncTearDown(self):
        await database.execute("DROP TRIGGER IF EXISTS test_reject_intake_item ON source_items")
        await database.execute("DROP FUNCTION IF EXISTS test_reject_intake_item()")
        await database.execute("DROP TRIGGER IF EXISTS test_reject_intake_document ON document_instances")
        await database.execute("DROP FUNCTION IF EXISTS test_reject_intake_document()")
        await database.execute("DROP TRIGGER IF EXISTS test_delay_intake_item ON source_items")
        await database.execute("DROP FUNCTION IF EXISTS test_delay_intake_item()")
        raw_ids = await database.fetch_all(
            "SELECT raw_asset_id FROM document_instances WHERE folder_id = :folder",
            {"folder": self.folder_id},
        )
        item_ids = await database.fetch_all(
            "SELECT id FROM source_items WHERE source_id = :source", {"source": self.source_id}
        )
        await database.execute("DELETE FROM source_items WHERE source_id = :source", {"source": self.source_id})
        await database.execute("DELETE FROM document_instances WHERE folder_id = :folder", {"folder": self.folder_id})
        asset_ids = {row["raw_asset_id"] for row in raw_ids if row["raw_asset_id"]}
        asset_ids.update(f"ra_{row['id'][3:]}" for row in item_ids)
        for asset_id in asset_ids:
            await database.execute("DELETE FROM raw_assets WHERE id = :id", {"id": asset_id})
        await database.execute("DELETE FROM folders WHERE id = :id", {"id": self.folder_id})
        await database.execute("DELETE FROM sources WHERE id = :id", {"id": self.source_id})
        await database.disconnect()
        self.storage.cleanup()

    async def test_failed_final_upload_write_leaves_no_document_or_file(self):
        await database.execute(
            "CREATE FUNCTION test_reject_intake_item() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected intake failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_intake_item BEFORE INSERT ON source_items "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_intake_item()"
        )
        upload = UploadFile(file=io.BytesIO(b"test content"), filename="test.txt")
        with patch.object(folders, "USER_DATA_DIR", Path(self.storage.name)):
            with self.assertRaises(Exception):
                await folders.upload_to_folder(
                    self.folder_id, [upload], captured_at=None, effective_at=None,
                    doc_kind=None, _={},
                )

        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ),
            0,
        )
        self.assertEqual(list(Path(self.storage.name).rglob("*.txt")), [])
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM raw_assets WHERE storage_key LIKE :path",
                {"path": f"{self.storage.name}%"},
            ),
            0,
        )

    async def test_failed_document_materialization_rolls_back_source_item(self):
        await database.execute(
            "CREATE FUNCTION test_reject_intake_document() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected document failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_intake_document BEFORE INSERT ON document_instances "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_intake_document()"
        )
        origin = f"https://example.test/{uuid4().hex}"
        with self.assertRaises(Exception):
            await sources.create_source_items(
                self.source_id,
                sources.SourceItemsCreate(items=[sources.SourceItemCreate(
                    origin_ref=origin, origin_ref_type="url", title="test"
                )]),
                _={},
            )
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM source_items WHERE source_id = :source",
                {"source": self.source_id},
            ),
            0,
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM raw_assets WHERE storage_key = :origin",
                {"origin": origin},
            ),
            0,
        )

    async def test_failed_legacy_source_upload_removes_new_file(self):
        await database.execute(
            "CREATE FUNCTION test_reject_intake_item() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected intake failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_intake_item BEFORE INSERT ON source_items "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_intake_item()"
        )
        upload = UploadFile(file=io.BytesIO(b"legacy upload"), filename="legacy.txt")
        with patch.object(sources, "USER_DATA_DIR", Path(self.storage.name)):
            with self.assertRaises(Exception):
                await sources.upload_to_source(
                    self.source_id, [upload], captured_at=None, effective_at=None,
                    doc_kind=None, _={},
                )
        self.assertEqual(list(Path(self.storage.name).rglob("*.txt")), [])

    async def test_partial_file_write_is_cleaned_before_database_intake(self):
        original_open = Path.open
        storage = self.storage.name

        class BrokenWriter:
            def __init__(self, path):
                self.file = original_open(path, "xb")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.file.close()

            def write(self, content):
                self.file.write(content[:3])
                raise OSError("injected file write failure")

        def broken_open(path, mode="r", *args, **kwargs):
            if mode == "xb" and str(path).startswith(storage):
                return BrokenWriter(path)
            return original_open(path, mode, *args, **kwargs)

        upload = UploadFile(file=io.BytesIO(b"test content"), filename="partial.txt")
        with patch.object(folders, "USER_DATA_DIR", Path(storage)), patch.object(Path, "open", broken_open):
            with self.assertRaisesRegex(OSError, "injected file write failure"):
                await folders.upload_to_folder(
                    self.folder_id, [upload], captured_at=None, effective_at=None,
                    doc_kind=None, _={},
                )
        self.assertEqual(list(Path(storage).rglob("*.txt")), [])
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ),
            0,
        )

    async def test_cleanup_failure_is_reported_after_database_rollback(self):
        await database.execute(
            "CREATE FUNCTION test_reject_intake_item() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected intake failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_intake_item BEFORE INSERT ON source_items "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_intake_item()"
        )
        upload = UploadFile(file=io.BytesIO(b"test content"), filename="cleanup.txt")
        with patch.object(folders, "USER_DATA_DIR", Path(self.storage.name)):
            with patch.object(Path, "unlink", side_effect=OSError("cleanup denied")):
                with self.assertRaisesRegex(OSError, "cleanup denied"):
                    await folders.upload_to_folder(
                        self.folder_id, [upload], captured_at=None, effective_at=None,
                        doc_kind=None, _={},
                    )
        self.assertEqual(len(list(Path(self.storage.name).rglob("*cleanup.txt"))), 1)
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ),
            0,
        )

    async def test_upload_filename_collision_preserves_existing_file(self):
        raw_dir = Path(self.storage.name) / "default" / "raw" / "plaintext"
        raw_dir.mkdir(parents=True)
        existing = raw_dir / f"{date.today()}-cafebabe-existing.txt"
        existing.write_bytes(b"original")
        upload = UploadFile(file=io.BytesIO(b"replacement"), filename="existing.txt")
        with patch.object(sources, "USER_DATA_DIR", Path(self.storage.name)):
            with patch.object(document_intake.secrets, "token_hex", return_value="cafebabe"):
                with self.assertRaises(FileExistsError):
                    await sources.upload_to_source(
                        self.source_id, [upload], captured_at=None, effective_at=None,
                        doc_kind=None, _={},
                    )
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM source_items WHERE source_id = :source",
                {"source": self.source_id},
            ),
            0,
        )

    async def test_repeated_connector_item_keeps_one_moved_document_and_terminal_state(self):
        await database.execute(
            "UPDATE sources SET type = 'rss', fetch_mode = 'subscription' WHERE id = :id",
            {"id": self.source_id},
        )
        connector_id = "con_" + self.source_id[4:]
        moved_folder = f"fld_moved_{uuid4().hex[:12]}"
        await database.execute(
            "INSERT INTO connectors (id, user_id, folder_id, type) "
            "VALUES (:id, 'default', :folder, 'rss')",
            {"id": connector_id, "folder": self.folder_id},
        )
        await database.execute(
            "INSERT INTO folders (id, user_id, name) "
            "VALUES (:id, 'default', 'moved')",
            {"id": moved_folder},
        )
        origin = f"https://example.test/{uuid4().hex}"
        body = sources.SourceItemsCreate(items=[sources.SourceItemCreate(
            origin_ref=origin, origin_ref_type="rss", title="First title"
        )])
        try:
            first = (await sources.create_source_items(self.source_id, body, _={}))['items'][0]
            document_id = first['document_instance_id']
            await database.execute(
                "UPDATE document_instances SET folder_id = :folder, status = 'ignored' WHERE id = :id",
                {"folder": moved_folder, "id": document_id},
            )
            await database.execute(
                "UPDATE source_items SET status = 'ignored' WHERE id = :id", {"id": first['id']}
            )
            second = (await sources.create_source_items(self.source_id, body, _={}))['items'][0]

            self.assertEqual(second['id'], first['id'])
            self.assertEqual(second['document_instance_id'], document_id)
            self.assertEqual(second['status'], 'ignored')
            document = await database.fetch_one(
                "SELECT folder_id, status, connector_id FROM document_instances WHERE id = :id",
                {"id": document_id},
            )
            self.assertEqual(
                (document['folder_id'], document['status'], document['connector_id']),
                (moved_folder, 'ignored', connector_id),
            )
            self.assertEqual(
                await database.fetch_val(
                    "SELECT COUNT(*) FROM source_items WHERE source_id = :source",
                    {"source": self.source_id},
                ),
                1,
            )
        finally:
            await database.execute("DELETE FROM source_items WHERE source_id = :source", {"source": self.source_id})
            await database.execute("DELETE FROM document_instances WHERE folder_id = :folder", {"folder": moved_folder})
            await database.execute("DELETE FROM raw_assets WHERE storage_key = :origin", {"origin": origin})
            await database.execute("DELETE FROM connectors WHERE id = :id", {"id": connector_id})
            await database.execute("DELETE FROM folders WHERE id = :id", {"id": moved_folder})

    async def test_simultaneous_connector_intake_returns_one_real_link(self):
        await database.execute(
            "UPDATE sources SET type = 'rss', fetch_mode = 'subscription' WHERE id = :id",
            {"id": self.source_id},
        )
        await database.execute(
            "CREATE FUNCTION test_delay_intake_item() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN PERFORM pg_sleep(0.15); RETURN NEW; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_delay_intake_item BEFORE INSERT ON source_items "
            "FOR EACH ROW EXECUTE FUNCTION test_delay_intake_item()"
        )
        source = {"id": self.source_id, "user_id": "default", "type": "rss",
                  "fetch_mode": "subscription"}
        item = {"origin_ref": f"https://example.test/{uuid4().hex}",
                "origin_ref_type": "rss", "title": "Concurrent item"}
        start = asyncio.Event()

        async def receive():
            await start.wait()
            return await document_intake.receive_source_item(source, item)

        first_task = asyncio.create_task(receive())
        second_task = asyncio.create_task(receive())
        start.set()
        first, second = await asyncio.wait_for(asyncio.gather(first_task, second_task), 5)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["document_instance_id"], second["document_instance_id"])
        self.assertIsNotNone(first["document_instance_id"])
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM source_items WHERE source_id = :source",
                {"source": self.source_id},
            ),
            1,
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ),
            1,
        )

    async def test_repeated_manual_file_creates_two_links_and_dispatches_after_commit(self):
        observed_counts = []
        source_id = self.source_id

        class TriggerClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, _url, timeout):
                observed_counts.append(await database.fetch_val(
                    "SELECT COUNT(*) FROM source_items WHERE source_id = :source",
                    {"source": source_id},
                ))

        with patch.object(folders, "USER_DATA_DIR", Path(self.storage.name)):
            with patch.object(folders.httpx, "AsyncClient", TriggerClient):
                first = await folders.upload_to_folder(
                    self.folder_id,
                    [UploadFile(file=io.BytesIO(b"same"), filename="same.txt")],
                    captured_at=None, effective_at=None, doc_kind=None, _={},
                )
                second = await folders.upload_to_folder(
                    self.folder_id,
                    [UploadFile(file=io.BytesIO(b"same"), filename="same.txt")],
                    captured_at=None, effective_at=None, doc_kind=None, _={},
                )
        self.assertNotEqual(first["items"][0], second["items"][0])
        self.assertEqual(observed_counts, [1, 2])
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ),
            2,
        )
        self.assertEqual(len(list(Path(self.storage.name).rglob("*.txt"))), 2)

    async def test_repeated_folder_url_reuses_link_without_requeue(self):
        await database.execute(
            "UPDATE sources SET type = 'url' WHERE id = :id", {"id": self.source_id}
        )
        origin = f"https://example.test/{uuid4().hex}"
        triggered = []

        async def trigger(source_id):
            triggered.append(source_id)
            return True

        with patch.object(folders, "_trigger_ingestion", trigger):
            first = await folders.add_url_to_folder(
                self.folder_id, {"urls": [origin], "doc_kind": "news"}, _={}
            )
            item = first["items"][0]
            await database.execute(
                "UPDATE source_items SET status = 'succeeded' WHERE id = :id",
                {"id": item["source_item_id"]},
            )
            second = await folders.add_url_to_folder(
                self.folder_id, {"urls": [origin], "doc_kind": "news"}, _={}
            )
            await database.execute(
                "UPDATE source_items SET status = 'failed' WHERE id = :id",
                {"id": item["source_item_id"]},
            )
            third = await folders.add_url_to_folder(
                self.folder_id, {"urls": [origin], "doc_kind": "news"}, _={}
            )

        self.assertEqual(second["items"], first["items"])
        self.assertEqual(third["items"], first["items"])
        self.assertEqual((first["urls_queued"], first["urls_reused"]), (1, 0))
        self.assertEqual((second["urls_queued"], second["urls_reused"]), (0, 1))
        self.assertEqual((third["urls_queued"], third["urls_reused"]), (0, 1))
        self.assertEqual(triggered, [self.source_id])
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ), 1,
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM raw_assets WHERE storage_key = :origin",
                {"origin": origin},
            ), 1,
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM source_items WHERE id = :id",
                {"id": item["source_item_id"]},
            ), "failed",
        )

    async def test_folder_url_failure_rolls_back_and_archived_duplicate_stays_archived(self):
        await database.execute(
            "UPDATE sources SET type = 'url' WHERE id = :id", {"id": self.source_id}
        )
        origin = f"https://example.test/{uuid4().hex}"
        await database.execute(
            "CREATE FUNCTION test_reject_intake_document() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected document failure'; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_intake_document BEFORE INSERT ON document_instances "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_intake_document()"
        )
        with self.assertRaisesRegex(Exception, "injected document failure"):
            await folders.add_url_to_folder(self.folder_id, {"urls": [origin]}, _={})
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM source_items WHERE source_id = :source",
                {"source": self.source_id},
            ), 0,
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM raw_assets WHERE storage_key = :origin",
                {"origin": origin},
            ), 0,
        )
        await database.execute("DROP TRIGGER test_reject_intake_document ON document_instances")
        await database.execute("DROP FUNCTION test_reject_intake_document()")

        with patch.object(folders, "_trigger_ingestion", return_value=True):
            first = await folders.add_url_to_folder(self.folder_id, {"urls": [origin]}, _={})
            item = first["items"][0]
            await database.execute(
                "UPDATE source_items SET status = 'ignored' WHERE id = :id",
                {"id": item["source_item_id"]},
            )
            await database.execute(
                "UPDATE document_instances SET status = 'ignored' WHERE id = :id",
                {"id": item["document_instance_id"]},
            )
            with self.assertRaises(HTTPException) as raised:
                await folders.add_url_to_folder(self.folder_id, {"urls": [origin]}, _={})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ), 1,
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM source_items WHERE id = :id",
                {"id": item["source_item_id"]},
            ), "ignored",
        )

    async def test_simultaneous_folder_url_adds_share_one_document(self):
        await database.execute(
            "UPDATE sources SET type = 'url' WHERE id = :id", {"id": self.source_id}
        )
        await database.execute(
            "CREATE FUNCTION test_delay_intake_item() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN PERFORM pg_sleep(0.15); RETURN NEW; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_delay_intake_item BEFORE INSERT ON source_items "
            "FOR EACH ROW EXECUTE FUNCTION test_delay_intake_item()"
        )
        origin = f"https://example.test/{uuid4().hex}"
        start = asyncio.Event()

        async def add():
            await start.wait()
            return await folders.add_url_to_folder(
                self.folder_id, {"urls": [origin]}, _={}
            )

        with patch.object(folders, "_trigger_ingestion", return_value=True):
            first_task = asyncio.create_task(add())
            second_task = asyncio.create_task(add())
            start.set()
            first, second = await asyncio.wait_for(
                asyncio.gather(first_task, second_task), 5
            )
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(first["urls_queued"] + second["urls_queued"], 1)
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ), 1,
        )

    async def test_legacy_source_url_duplicate_does_not_trigger_again(self):
        await database.execute(
            "UPDATE sources SET type = 'url' WHERE id = :id", {"id": self.source_id}
        )
        origin = f"https://example.test/{uuid4().hex}"
        triggered = []

        async def trigger(source_id):
            triggered.append(source_id)
            return True

        with patch.object(sources, "_trigger_ingestion", trigger):
            first = await sources.add_url_to_source(
                self.source_id, {"urls": [origin]}, _={}
            )
            second = await sources.add_url_to_source(
                self.source_id, {"urls": [origin]}, _={}
            )
        self.assertEqual(first["source_items"][0]["id"], second["source_items"][0]["id"])
        self.assertEqual((first["urls_queued"], first["urls_reused"]), (1, 0))
        self.assertEqual((second["urls_queued"], second["urls_reused"]), (0, 1))
        self.assertEqual(triggered, [self.source_id])

    async def test_second_file_failure_keeps_first_pending_and_cleans_second(self):
        await database.execute(
            "CREATE FUNCTION test_reject_intake_item() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.title = 'second' THEN RAISE EXCEPTION 'injected second failure'; "
            "END IF; RETURN NEW; END $$"
        )
        await database.execute(
            "CREATE TRIGGER test_reject_intake_item BEFORE INSERT ON source_items "
            "FOR EACH ROW EXECUTE FUNCTION test_reject_intake_item()"
        )
        files = [
            UploadFile(file=io.BytesIO(b"first"), filename="first.txt"),
            UploadFile(file=io.BytesIO(b"second"), filename="second.txt"),
        ]
        with patch.object(folders, "USER_DATA_DIR", Path(self.storage.name)):
            with self.assertRaisesRegex(Exception, "injected second failure"):
                await folders.upload_to_folder(
                    self.folder_id, files, captured_at=None, effective_at=None,
                    doc_kind=None, _={},
                )
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM source_items WHERE source_id = :source AND status = 'pending'",
                {"source": self.source_id},
            ),
            1,
        )
        self.assertEqual(len(list(Path(self.storage.name).rglob("*first.txt"))), 1)
        self.assertEqual(list(Path(self.storage.name).rglob("*second.txt")), [])

    async def test_failed_trigger_keeps_committed_upload_pending(self):
        class FailingTriggerClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, _url, timeout):
                raise OSError("worker unavailable")

        upload = UploadFile(file=io.BytesIO(b"test"), filename="pending.txt")
        with patch.object(folders, "USER_DATA_DIR", Path(self.storage.name)):
            with patch.object(folders.httpx, "AsyncClient", FailingTriggerClient):
                result = await folders.upload_to_folder(
                    self.folder_id, [upload], captured_at=None, effective_at=None,
                    doc_kind=None, _={},
                )
        self.assertTrue(result["ok"])
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM source_items WHERE id = :id",
                {"id": result["items"][0]["source_item_id"]},
            ),
            "pending",
        )

    async def test_legacy_source_without_folder_keeps_unlinked_item(self):
        await database.execute("DELETE FROM folders WHERE id = :id", {"id": self.folder_id})
        result = await sources.create_source_items(
            self.source_id,
            sources.SourceItemsCreate(items=[sources.SourceItemCreate(
                origin_ref=f"https://example.test/{uuid4().hex}", origin_ref_type="url"
            )]),
            _={},
        )
        self.assertIsNone(result["items"][0]["document_instance_id"])
        self.assertEqual(
            await database.fetch_val("SELECT COUNT(*) FROM document_instances WHERE user_id = 'default' AND folder_id = :folder", {"folder": self.folder_id}),
            0,
        )

    async def test_repeated_terminal_unlinked_item_is_not_materialized(self):
        await database.execute("DELETE FROM folders WHERE id = :id", {"id": self.folder_id})
        origin = f"https://example.test/{uuid4().hex}"
        body = sources.SourceItemsCreate(items=[sources.SourceItemCreate(
            origin_ref=origin, origin_ref_type="url"
        )])
        first = (await sources.create_source_items(self.source_id, body, _={}))["items"][0]
        await database.execute(
            "UPDATE source_items SET status = 'ignored' WHERE id = :id", {"id": first["id"]}
        )
        await database.execute(
            "INSERT INTO folders (id, user_id, name) VALUES (:id, 'default', 'test')",
            {"id": self.folder_id},
        )
        second = (await sources.create_source_items(self.source_id, body, _={}))["items"][0]
        self.assertEqual(second["status"], "ignored")
        self.assertIsNone(second["document_instance_id"])
        self.assertEqual(
            await database.fetch_val(
                "SELECT COUNT(*) FROM document_instances WHERE folder_id = :folder",
                {"folder": self.folder_id},
            ),
            0,
        )

    async def test_new_terminal_source_item_keeps_existing_materialization_contract(self):
        result = await sources.create_source_items(
            self.source_id,
            sources.SourceItemsCreate(items=[sources.SourceItemCreate(
                origin_ref=f"https://example.test/{uuid4().hex}",
                origin_ref_type="url", status="ignored",
            )]),
            _={},
        )
        document_id = result["items"][0]["document_instance_id"]
        self.assertIsNotNone(document_id)
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM document_instances WHERE id = :id", {"id": document_id}
            ),
            "ignored",
        )

    async def test_historical_unlinked_document_is_reported_without_state_change(self):
        await database.execute("DELETE FROM folders WHERE id = :id", {"id": self.folder_id})
        origin = f"https://example.test/{uuid4().hex}"
        body = sources.SourceItemsCreate(items=[sources.SourceItemCreate(
            origin_ref=origin, origin_ref_type="url"
        )])
        item = (await sources.create_source_items(self.source_id, body, _={}))["items"][0]
        await database.execute(
            "INSERT INTO folders (id, user_id, name) VALUES (:id, 'default', 'test')",
            {"id": self.folder_id},
        )
        orphan_document = "di_" + item["id"][3:]
        await database.execute(
            "INSERT INTO document_instances (id, user_id, folder_id, status) "
            "VALUES (:id, 'default', :folder, 'processing')",
            {"id": orphan_document, "folder": self.folder_id},
        )
        with self.assertRaises(Exception):
            await sources.create_source_items(self.source_id, body, _={})
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM document_instances WHERE id = :id",
                {"id": orphan_document},
            ),
            "processing",
        )
        self.assertIsNone(await database.fetch_val(
            "SELECT document_instance_id FROM source_items WHERE id = :id",
            {"id": item["id"]},
        ))

    async def test_repeated_intake_and_archive_do_not_revive_document(self):
        source = {"id": self.source_id, "user_id": "default", "type": "plaintext"}
        item = {"origin_ref": f"https://example.test/{uuid4().hex}",
                "origin_ref_type": "url", "title": "Before archive"}
        first = await document_intake.receive_source_item(source, item)
        document_id = first["document_instance_id"]
        await database.execute(
            "UPDATE source_items SET status = 'succeeded' WHERE id = :id", {"id": first["id"]}
        )
        await database.execute(
            "UPDATE document_instances SET status = 'succeeded' WHERE id = :id",
            {"id": document_id},
        )
        start = asyncio.Event()

        async def repeat():
            await start.wait()
            return await document_intake.receive_source_item(source, {**item, "title": "After archive"})

        async def archive():
            await start.wait()
            return await document_lifecycle.archive_document(document_id)

        repeated_task = asyncio.create_task(repeat())
        archive_task = asyncio.create_task(archive())
        start.set()
        repeated, archived = await asyncio.wait_for(
            asyncio.gather(repeated_task, archive_task), 5
        )
        self.assertEqual(archived[0], "archived")
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM document_instances WHERE id = :id", {"id": document_id}
            ),
            "ignored",
        )
        self.assertEqual(
            await database.fetch_val(
                "SELECT status FROM source_items WHERE id = :id", {"id": first["id"]}
            ),
            "ignored",
        )

    async def test_uploaded_document_can_be_deleted_with_its_file(self):
        class TriggerClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, _url, timeout):
                return None

        with patch.object(folders, "USER_DATA_DIR", Path(self.storage.name)):
            with patch.object(folders.httpx, "AsyncClient", TriggerClient):
                result = await folders.upload_to_folder(
                    self.folder_id,
                    [UploadFile(file=io.BytesIO(b"delete me"), filename="delete.txt")],
                    captured_at=None, effective_at=None, doc_kind=None, _={},
                )
        document_id = result["items"][0]["document_instance_id"]
        self.assertEqual(len(list(Path(self.storage.name).rglob("*delete.txt"))), 1)
        with patch.object(document_lifecycle, "USER_DATA_DIR", Path(self.storage.name)):
            deletion = await document_lifecycle.delete_document(document_id)
        self.assertEqual(deletion["status"], "deleted")
        self.assertEqual(deletion["file_warnings"], [])
        self.assertEqual(list(Path(self.storage.name).rglob("*delete.txt")), [])

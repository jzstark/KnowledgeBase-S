import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from routers import folders
import document_lifecycle


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ArchiveDatabase:
    def __init__(self, document_status: str, source_statuses: list[str]):
        self.document_status = document_status
        self.source_statuses = source_statuses
        self.executed: list[tuple[str, dict]] = []

    def transaction(self):
        return _Transaction()

    async def fetch_one(self, query, values):
        return {
            "id": values["id"],
            "folder_id": "fld_test",
            "status": self.document_status,
        }

    async def fetch_all(self, query, values):
        return [
            {"id": f"si_{index}", "status": status}
            for index, status in enumerate(self.source_statuses)
        ]

    async def execute(self, query, values):
        self.executed.append((query, values))


class ArchiveDocumentInstanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_archive_updates_document_and_source_item(self):
        fake = _ArchiveDatabase("succeeded", ["succeeded"])

        with patch.object(folders.database, "database", fake):
            status, detail = await document_lifecycle._archive_document_instance(
                "di_test", folder_id="fld_test"
            )

        self.assertEqual((status, detail), ("archived", "归档成功"))
        self.assertEqual(len(fake.executed), 2)
        self.assertIn("UPDATE source_items", fake.executed[0][0])
        self.assertIn("UPDATE document_instances", fake.executed[1][0])

    async def test_archive_rejects_processing_source_item_without_writes(self):
        fake = _ArchiveDatabase("pending", ["processing"])

        with patch.object(folders.database, "database", fake):
            status, detail = await document_lifecycle._archive_document_instance(
                "di_test", folder_id="fld_test"
            )

        self.assertEqual(status, "failed")
        self.assertIn("正在处理", detail)
        self.assertEqual(fake.executed, [])

    async def test_archive_is_idempotent(self):
        fake = _ArchiveDatabase("ignored", ["ignored"])

        with patch.object(folders.database, "database", fake):
            status, detail = await document_lifecycle._archive_document_instance(
                "di_test", folder_id="fld_test"
            )

        self.assertEqual((status, detail), ("skipped", "文档已经归档"))


class _FolderCountDatabase:
    def __init__(self, *, current_count: int, total_count: int):
        self.current_count = current_count
        self.total_count = total_count
        self.executed: list[tuple[str, dict]] = []

    async def fetch_one(self, query, values):
        return {
            "id": values["id"],
            "user_id": "default",
            "name": "Test folder",
            "kind": "normal",
            "status": "active",
            "created_at": None,
            "updated_at": None,
        }

    async def fetch_val(self, query, values):
        return (
            self.current_count
            if "status NOT IN ('ignored', 'deleted')" in query
            else self.total_count
        )

    async def execute(self, query, values):
        self.executed.append((query, values))


class FolderCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_folder_detail_counts_only_current_documents(self):
        fake = _FolderCountDatabase(current_count=2, total_count=4)

        with patch.object(folders.database, "database", fake):
            result = await folders.get_folder("fld_test", _={})

        self.assertEqual(result["item_count"], 2)

    async def test_folder_with_only_archived_or_deleted_documents_can_be_removed(self):
        fake = _FolderCountDatabase(current_count=0, total_count=2)

        with patch.object(folders.database, "database", fake):
            await folders.delete_folder("fld_test", _={})

        self.assertEqual(len(fake.executed), 2)
        self.assertIn("UPDATE folders", fake.executed[0][0])
        self.assertIn("UPDATE sources", fake.executed[1][0])


if __name__ == "__main__":
    unittest.main()

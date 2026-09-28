import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/app")
os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")

from routers import files


class FilesTests(unittest.IsolatedAsyncioTestCase):
    async def test_tree_only_lists_raw_and_wiki(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "default"
            (base / "wiki" / "articles").mkdir(parents=True)
            (base / "wiki" / "articles" / "art_1.md").write_text("article", encoding="utf-8")
            (base / "config" / "templates").mkdir(parents=True)
            (base / "config" / "topics.md").write_text("topics", encoding="utf-8")
            (base / "config" / "templates" / "公众号新闻.md").write_text("template", encoding="utf-8")

            with patch.object(files, "USER_DATA_DIR", Path(tmp)):
                tree = await files.get_tree()
                wiki = await files.get_content("wiki/articles/art_1.md")
                with self.assertRaises(HTTPException) as error:
                    await files.get_content("config/topics.md")

        self.assertEqual(set(tree), {"raw", "wiki"})
        self.assertEqual(wiki, {"content": "article"})
        self.assertEqual(error.exception.status_code, 403)

    def test_file_content_is_read_only(self):
        routes = [route for route in files.router.routes if route.path == "/api/files/content"]
        self.assertEqual([route.methods for route in routes], [{"GET"}])

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sources.base import RawItem  # noqa: E402
from sources.dispatch import is_book_source_item, source_for_item  # noqa: E402


class SourceDispatchTest(unittest.TestCase):
    def test_uploaded_file_uses_item_parser_in_mixed_folder(self):
        default_source = object()
        source_config = {"id": "src_folder", "type": "url"}
        source_item = {"source_type": "plaintext"}

        selected_source, selected_type = source_for_item(
            default_source, source_config, source_item
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "document.txt"
            path.write_text("A personal document with enough text for extraction.", encoding="utf-8")
            item = RawItem(
                source_id="src_folder",
                title="document",
                raw_ref={"type": "file", "path": str(path)},
                content_type="application/octet-stream",
                raw_bytes=None,
            )

            self.assertEqual(selected_type, "plaintext")
            self.assertEqual(
                selected_source.extract_text(item),
                "A personal document with enough text for extraction.",
            )

    def test_matching_item_type_reuses_default_source(self):
        default_source = object()
        selected_source, selected_type = source_for_item(
            default_source,
            {"id": "src_url", "type": "url"},
            {"source_type": "url"},
        )

        self.assertIs(selected_source, default_source)
        self.assertEqual(selected_type, "url")

    def test_book_item_is_routed_to_book_pipeline(self):
        self.assertTrue(
            is_book_source_item(
                {"id": "src_folder", "type": "url"},
                {"source_type": "epub"},
            )
        )


class PipelineDispatchTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _load_pipeline():
        os.environ.setdefault("API_BASE_URL", "http://api:8000")
        os.environ.setdefault("CLAUDE_API_KEY", "test")
        os.environ.setdefault("OPENAI_API_KEY", "test")

        anthropic = types.ModuleType("anthropic")
        anthropic.Anthropic = lambda **_: object()
        httpx = types.ModuleType("httpx")
        openai = types.ModuleType("openai")
        openai.AsyncOpenAI = lambda **_: object()
        trafilatura = types.ModuleType("trafilatura")
        yaml = types.ModuleType("yaml")
        yaml.safe_load = lambda _: {}

        with patch.dict(
            sys.modules,
            {
                "anthropic": anthropic,
                "httpx": httpx,
                "openai": openai,
                "trafilatura": trafilatura,
                "yaml": yaml,
            },
        ):
            import pipeline

        return pipeline

    async def test_pipeline_routes_epub_item_to_book_pipeline(self):
        pipeline = self._load_pipeline()
        source_item = {
            "id": "si_book",
            "source_id": "src_folder",
            "source_type": "epub",
        }

        with (
            patch.object(
                pipeline,
                "fetch_pending_source_items",
                AsyncMock(return_value=[source_item]),
            ),
            patch.object(pipeline, "run_book_pipeline", AsyncMock()) as run_book,
            patch.object(pipeline, "update_last_fetched", AsyncMock()),
            patch.object(pipeline, "refresh_stale_entities", AsyncMock()),
        ):
            await pipeline.run_pipeline(
                object(),
                {"id": "src_folder", "type": "url"},
            )

        self.assertEqual(run_book.await_args.kwargs["pending_items"], [source_item])
        self.assertFalse(run_book.await_args.kwargs["finalize"])

    async def test_pipeline_uses_uploaded_item_type_instead_of_folder_type(self):
        pipeline = self._load_pipeline()

        class WrongDefaultSource:
            def extract_text(self, _):
                raise AssertionError("folder URL parser must not process uploaded files")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "document.txt"
            path.write_text("Personal document text. " * 10, encoding="utf-8")
            source_item = {
                "id": "si_document",
                "source_id": "src_folder",
                "source_type": "plaintext",
                "origin_ref": "upload://document.txt",
                "origin_ref_type": "upload",
                "raw_snapshot_ref": str(path),
                "title": "document",
                "document_instance_id": "di_document",
            }
            result = types.SimpleNamespace(
                entities=[], article_id="art_document", summary_id="sum_document"
            )

            with (
                patch.object(
                    pipeline,
                    "fetch_pending_source_items",
                    AsyncMock(return_value=[source_item]),
                ),
                patch.object(pipeline, "update_source_item_status", AsyncMock()) as update_status,
                patch.object(pipeline, "save_extracted_text", return_value="/tmp/extracted.txt"),
                patch.object(pipeline, "save_raw", return_value=str(path)),
                patch.object(
                    pipeline,
                    "process_article_like_item",
                    AsyncMock(return_value=result),
                ) as process_item,
                patch.object(pipeline, "update_last_fetched", AsyncMock()),
                patch.object(pipeline, "refresh_stale_entities", AsyncMock()),
            ):
                await pipeline.run_pipeline(
                    WrongDefaultSource(),
                    {"id": "src_folder", "type": "url"},
                )

            ingestion_input = process_item.await_args.args[0]
            self.assertEqual(ingestion_input.source_type, "plaintext")
            self.assertEqual(ingestion_input.raw_ref, {"type": "file", "path": str(path)})
            self.assertEqual(update_status.await_args_list[-1].args, ("si_document", "succeeded"))


if __name__ == "__main__":
    unittest.main()

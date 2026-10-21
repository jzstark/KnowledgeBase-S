import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm  # noqa: E402
from sources.base import RawItem  # noqa: E402
from sources.image import ImageSource  # noqa: E402


class ExtractionDeferralTest(unittest.TestCase):
    def test_image_extraction_propagates_pending_batch_instead_of_empty_text(self):
        from datetime import datetime, timezone
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "page.png")
            Image.new("RGB", (40, 40), "white").save(path)
            item = RawItem(
                source_id="src", title="page", raw_ref={"type": "file", "path": path},
                content_type="image/png", raw_bytes=None,
                fetched_at=datetime.now(timezone.utc), source_item_id="si_1",
            )
            calls = []

            def pending(source_item_id, batch):
                calls.append((source_item_id, [key for key, _ in batch]))
                raise llm.Deferred

            with patch.object(llm, "call_many", pending):
                with self.assertRaises(llm.Deferred):
                    ImageSource("src", []).extract_text(item)
        self.assertEqual(calls, [("si_1", ["ocr:0"])])


if __name__ == "__main__":
    unittest.main()

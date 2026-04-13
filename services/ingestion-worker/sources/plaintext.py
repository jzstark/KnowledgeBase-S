"""
Plaintext Source — 直接读取纯文本文件（.txt / .md）。
"""

from pathlib import Path

from sources.base import RawItem
from sources.file_base import FileSourceMixin


class PlaintextSource(FileSourceMixin):
    content_type = "text/plain"

    def __init__(self, source_id: str, uploads: list[dict]):
        self.source_id = source_id
        self.uploads = uploads

    def extract_text(self, raw: RawItem) -> str:
        try:
            return Path(raw.raw_ref["path"]).read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            return ""

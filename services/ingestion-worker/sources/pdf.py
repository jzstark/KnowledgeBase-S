"""
PDF Source — 使用 PyMuPDF 提取文本。
"""

import fitz  # PyMuPDF

from sources.base import RawItem
from sources.file_base import FileSourceMixin


class PDFSource(FileSourceMixin):
    content_type = "application/pdf"

    def __init__(self, source_id: str, uploads: list[dict]):
        self.source_id = source_id
        self.uploads = uploads

    def extract_text(self, raw: RawItem) -> str:
        try:
            doc = fitz.open(raw.raw_ref["path"])
            pages = [page.get_text() for page in doc]
            doc.close()
            return "\n\n".join(pages).strip()
        except Exception:
            return ""

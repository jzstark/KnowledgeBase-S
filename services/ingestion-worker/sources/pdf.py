"""
PDF Source — 使用 PyMuPDF 提取文本，再用 Claude 清洗排版噪音。
"""

import os
from pathlib import Path

import anthropic
import fitz  # PyMuPDF

from sources.base import RawItem
from sources.file_base import FileSourceMixin

_CLEANUP_PROMPT = (
    Path(__file__).parent.parent / "config" / "pdf_cleanup.md"
).read_text(encoding="utf-8").strip()

_claude = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])


def _cleanup(raw_text: str) -> str:
    """用 LLM 清洗 PDF 提取的原文，去除排版噪音，保留正文。"""
    msg = _claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"{_CLEANUP_PROMPT}\n\n---\n\n{raw_text}",
        }],
    )
    return msg.content[0].text.strip()


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
            raw_text = "\n\n".join(pages).strip()
            if not raw_text:
                return ""
            return _cleanup(raw_text)
        except Exception:
            return ""

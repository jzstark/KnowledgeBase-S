"""
PDF Source — 使用 PyMuPDF 提取文本，再用 Claude 清洗排版噪音。
"""

import os

import anthropic
import fitz  # PyMuPDF

from sources.base import RawItem, message_text
from settings import settings
from prompts import prompts
from sources.file_base import FileSourceMixin

_claude = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])


def _cleanup(raw_text: str) -> str:
    """用 LLM 清洗 PDF 提取的原文，去除排版噪音，保留正文。"""
    msg = _claude.messages.create(
        model=settings.models.pdf_cleanup,
        max_tokens=settings.llm_output_tokens.pdf_cleanup,
        messages=[{
            "role": "user",
            "content": f"{prompts.pdf_cleanup()}\n\n---\n\n{raw_text}",
        }],
    )
    return message_text(msg)


class PDFSource(FileSourceMixin):
    content_type = "application/pdf"

    def __init__(self, source_id: str, uploads: list[dict]):
        self.source_id = source_id
        self.uploads = uploads

    def extract_text(self, raw: RawItem) -> str:
        try:
            doc = fitz.open(raw.raw_ref["path"])

            # PDF 元数据标题优先
            meta_title = (doc.metadata.get("title") or "").strip()
            if meta_title:
                raw.title = meta_title

            pages = [page.get_text() for page in doc]
            doc.close()
            raw_text = "\n\n".join(pages).strip()
            if not raw_text:
                return ""
            return _cleanup(raw_text)
        except Exception:
            return ""

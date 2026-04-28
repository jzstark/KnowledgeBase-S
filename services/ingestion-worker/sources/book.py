"""
Book Source — EPUB / MOBI 书籍入库。
每本书解析为若干章节，由 run_book_pipeline 创建 index + article 子节点。
"""

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sources.base import RawItem
from sources.file_base import FileSourceMixin

logger = logging.getLogger(__name__)

MIN_CHAPTER_CHARS = 300   # 低于此字符数的章节视为封面/目录等，跳过


@dataclass
class BookChapter:
    title: str
    text: str
    order: int


def _html_to_text(html_str: str) -> str:
    """Strip HTML tags, decode entities, normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_chapter_title(html_str: str) -> str | None:
    m = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html_str, re.IGNORECASE | re.DOTALL)
    if m:
        title = _html_to_text(m.group(1))
        if title and len(title) >= 2:
            return title[:120]
    return None


class BookSource(FileSourceMixin):
    """Handles .epub (primary) and .mobi (best-effort) files."""

    content_type = "application/epub+zip"

    def __init__(self, source_id: str, uploads: list[dict]):
        self.source_id = source_id
        self.uploads = uploads

    def extract_text(self, raw: RawItem) -> str:
        """Concatenated chapter text — used only if regular pipeline is called by mistake."""
        chapters = self.extract_chapters(raw)
        return "\n\n".join(ch.text for ch in chapters)

    def extract_chapters(self, raw: RawItem) -> list[BookChapter]:
        path = raw.raw_ref["path"]
        ext = Path(path).suffix.lower()
        if ext == ".epub":
            return self._parse_epub(path, raw)
        if ext in (".mobi", ".azw3"):
            return self._parse_mobi(path, raw)
        logger.warning(f"BookSource: unsupported extension '{ext}' for {path}")
        return []

    # ── EPUB ─────────────────────────────────────────────────────────────────

    def _parse_epub(self, path: str, raw: RawItem) -> list[BookChapter]:
        try:
            from ebooklib import epub, ITEM_DOCUMENT
        except ImportError:
            logger.error("ebooklib not installed — cannot parse EPUB")
            return []

        try:
            book = epub.read_epub(path, options={"ignore_ncx": True})
        except Exception as e:
            logger.error(f"epub.read_epub failed for {path}: {e}")
            return []

        title = (book.title or "").strip() or Path(path).stem
        raw.title = title

        # Spine order → correct reading sequence
        spine_ids = {item_id for item_id, _ in (book.spine or [])}
        id_to_item = {
            item.get_id(): item
            for item in book.get_items_of_type(ITEM_DOCUMENT)
        }

        # If spine is empty fall back to all document items
        if spine_ids:
            ordered = [id_to_item[sid] for sid in
                       [item_id for item_id, _ in book.spine]
                       if sid in id_to_item]
        else:
            ordered = list(id_to_item.values())

        chapters: list[BookChapter] = []
        order = 0
        for item in ordered:
            content = item.get_content()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="ignore")

            text = _html_to_text(content)
            if len(text) < MIN_CHAPTER_CHARS:
                continue

            ch_title = _extract_chapter_title(content) or f"第{order + 1}章"
            chapters.append(BookChapter(title=ch_title, text=text, order=order))
            order += 1

        logger.info(f"EPUB parsed: {path!r} → {len(chapters)} chapters")
        return chapters

    # ── MOBI (best-effort) ────────────────────────────────────────────────────

    def _parse_mobi(self, path: str, raw: RawItem) -> list[BookChapter]:
        try:
            import mobi
        except ImportError:
            logger.warning("mobi package not installed — cannot parse MOBI; convert to EPUB first")
            return []

        try:
            import shutil
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                _, filepath = mobi.extract(path)
                html_content = Path(filepath).read_text(encoding="utf-8", errors="ignore")
            text = _html_to_text(html_content)
        except Exception as e:
            logger.error(f"MOBI extraction failed for {path}: {e}")
            return []

        if len(text) < MIN_CHAPTER_CHARS:
            return []

        raw.title = raw.title or Path(path).stem
        return [BookChapter(title=raw.title, text=text, order=0)]

import hashlib
from datetime import datetime, timezone

import feedparser
import trafilatura

from .base import BaseSource, RawItem


def _parse_date(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc)
    return None


class RSSSource(BaseSource):
    fetch_mode = "subscription"

    def __init__(self, source_id: str, feed_url: str):
        self.source_id = source_id
        self.feed_url = feed_url

    def fetch_new_items(self, last_fetched_at: datetime | None) -> list[RawItem]:
        feed = feedparser.parse(self.feed_url)
        items: list[RawItem] = []

        for entry in feed.entries:
            pub = _parse_date(entry)
            if last_fetched_at and pub and pub <= last_fetched_at:
                continue

            url = entry.get("link", "")
            guid = entry.get("id", url)
            guid_hash = hashlib.md5(guid.encode()).hexdigest()[:8]
            date_str = (pub or datetime.utcnow()).strftime("%Y-%m-%d")
            title = entry.get("title", "")

            # 优先用 entry 自带 content，否则用 summary 字段
            content = ""
            if entry.get("content"):
                content = entry.content[0].value
            elif entry.get("summary"):
                content = entry.summary

            items.append(
                RawItem(
                    source_id=self.source_id,
                    title=title,
                    raw_ref={"type": "url", "url": url},
                    content_type="text/html",
                    raw_bytes=content.encode("utf-8") if content else None,
                    fetched_at=pub or datetime.utcnow(),
                )
            )
            # 存文件路径附加到 raw_ref（pipeline 保存后回填）
            items[-1]._file_name = f"{date_str}-{guid_hash}.html"

        return items

    def extract_text(self, raw: RawItem) -> str:
        if raw.raw_bytes:
            text = trafilatura.extract(raw.raw_bytes.decode("utf-8", errors="ignore"))
            if text:
                return text
            # fallback：去掉 HTML 标签
            import re
            return re.sub(r"<[^>]+>", " ", raw.raw_bytes.decode("utf-8", errors="ignore")).strip()
        return ""

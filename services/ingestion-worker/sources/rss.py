import hashlib
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import trafilatura

from .base import BaseSource, RawItem
from settings import settings

RSS_LOOKBACK_DAYS = settings.ingestion.rss_lookback_days


def _parse_struct_time(value) -> datetime | None:
    t = value
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc)
    return None


class RSSSource(BaseSource):
    fetch_mode = "subscription"

    def __init__(self, source_id: str, feed_url: str):
        self.source_id = source_id
        self.feed_url = feed_url

    def _fetch_feed(self):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=30,
                headers={
                    "User-Agent": "KnowledgeBase-S ingestion-worker/1.0",
                    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                },
            ) as client:
                resp = client.get(self.feed_url)
                resp.raise_for_status()
        except httpx.TooManyRedirects as exc:
            raise RuntimeError(f"RSS feed redirect loop: {self.feed_url}") from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"RSS feed returned HTTP {exc.response.status_code}: {self.feed_url}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"RSS feed request failed: {self.feed_url}: {exc}") from exc
        return feedparser.parse(resp.content)

    def fetch_new_items(self, last_fetched_at: datetime | None) -> list[RawItem]:
        feed = self._fetch_feed()
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError(f"RSS feed parse failed: {getattr(feed, 'bozo_exception', 'unknown error')}")

        items: list[RawItem] = []
        cutoff = last_fetched_at - timedelta(days=RSS_LOOKBACK_DAYS) if last_fetched_at else None

        for entry in feed.entries:
            published_at = _parse_struct_time(entry.get("published_parsed"))
            updated_at = _parse_struct_time(entry.get("updated_parsed"))
            item_time = published_at or updated_at
            # RSS providers can publish an item to the feed after its pubDate.
            # Keep a lookback window and rely on source_items uniqueness for dedupe.
            if cutoff and item_time and item_time <= cutoff:
                continue

            url = entry.get("link", "")
            guid = entry.get("id", url)
            guid_hash = hashlib.md5(guid.encode()).hexdigest()[:8]
            date_str = (item_time or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
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
                    fetched_at=item_time or datetime.now(timezone.utc),
                    source_published_at=published_at,
                    source_updated_at=updated_at,
                    captured_at=datetime.now(timezone.utc),
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

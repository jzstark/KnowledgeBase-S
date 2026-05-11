"""
URL Source — 一次性抓取单个网页。
"""

from datetime import datetime, timezone

import trafilatura

from sources.base import BaseSource, RawItem


def _parse_metadata_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class URLSource(BaseSource):
    fetch_mode = "one_shot"

    def __init__(self, source_id: str, url: str):
        self.source_id = source_id
        self.url = url

    def fetch_new_items(self, last_fetched_at=None) -> list[RawItem]:
        """抓取指定 URL。URL source 是手动触发型，每次触发都重新抓取。"""
        downloaded = trafilatura.fetch_url(self.url)
        if not downloaded:
            return []

        # 尝试提取标题
        metadata = trafilatura.extract_metadata(downloaded)
        title = metadata.title if metadata and metadata.title else self.url
        published_at = _parse_metadata_date(getattr(metadata, "date", None) if metadata else None)
        captured_at = datetime.now(timezone.utc)

        item = RawItem(
            source_id=self.source_id,
            title=title,
            raw_ref={"type": "url", "url": self.url},
            content_type="html",
            raw_bytes=downloaded.encode("utf-8"),
            fetched_at=published_at or captured_at,
            source_published_at=published_at,
            captured_at=captured_at,
        )
        item._file_name = f"{captured_at.strftime('%Y-%m-%d')}-{self.source_id}.html"
        return [item]

    def extract_text(self, raw: RawItem) -> str:
        text = trafilatura.extract(raw.raw_bytes.decode("utf-8"))
        return text or ""

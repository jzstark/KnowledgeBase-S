"""
WeChat Source — push 型，内容由 iPhone 快捷指令推送至 /api/sources/wechat/ingest。

fetch_new_items 从 source.config.pending_items 读取待处理条目，
按 pushed_at 精确过滤已处理的历史推送。
extract_text 直接解码纯文本（推送内容已是正文）。
"""

from datetime import datetime, timezone
from pathlib import Path

from sources.base import BaseSource, RawItem


class WechatSource(BaseSource):
    fetch_mode = "push"

    def __init__(self, source_id: str, config: dict):
        self.source_id = source_id
        self.pending_items: list[dict] = config.get("pending_items", [])

    def fetch_new_items(self, last_fetched_at: datetime | None) -> list[RawItem]:
        items: list[RawItem] = []

        for item in self.pending_items:
            # 精确 datetime 过滤（pushed_at 是 ISO8601 字符串）
            if last_fetched_at and item.get("pushed_at"):
                try:
                    pushed_dt = datetime.fromisoformat(
                        item["pushed_at"].replace("Z", "+00:00")
                    )
                    lfa = last_fetched_at
                    # 统一 tzinfo：若 last_fetched_at 无时区则去掉 pushed_dt 时区
                    if lfa.tzinfo is None:
                        pushed_dt = pushed_dt.replace(tzinfo=None)
                    if pushed_dt <= lfa:
                        continue
                except ValueError:
                    pass  # 时间格式异常时保守处理，不过滤

            p = Path(item.get("file_path", ""))
            if not p.exists():
                continue

            raw_item = RawItem(
                source_id=self.source_id,
                title=item.get("title"),
                raw_ref={"type": "url", "url": item.get("url", "")},
                content_type="text/plain",
                raw_bytes=p.read_bytes(),
                fetched_at=datetime.now(timezone.utc),
            )
            raw_item._file_name = p.name
            items.append(raw_item)

        return items

    def extract_text(self, raw: RawItem) -> str:
        if raw.raw_bytes:
            return raw.raw_bytes.decode("utf-8", errors="replace").strip()
        return ""

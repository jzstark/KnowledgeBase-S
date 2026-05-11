"""
文件型 Source 共用 Mixin。

所有手动上传型 Source（pdf/image/plaintext/word）继承此类，
共用 fetch_new_items 逻辑：从 source.config.uploads 读取已保存到磁盘的文件路径，
返回 RawItem 列表。extract_text 由各子类自行实现。
"""

from datetime import datetime, timezone
from pathlib import Path

from sources.base import BaseSource, RawItem


def _parse_optional_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class FileSourceMixin(BaseSource):
    """
    子类需在 __init__ 中设置：
      self.source_id: str
      self.uploads: list[dict]   # [{"date": "YYYY-MM-DD", "files": ["/abs/path", ...]}, ...]
      self.content_type: str
    """

    fetch_mode = "manual"

    def fetch_new_items(self, last_fetched_at: datetime | None) -> list[RawItem]:
        items: list[RawItem] = []

        for batch in self.uploads:
            captured_at = _parse_optional_time(batch.get("captured_at"))
            effective_at = _parse_optional_time(batch.get("effective_at"))
            for abs_path in batch.get("files", []):
                p = Path(abs_path)
                if not p.exists():
                    continue

                # 用文件 mtime 判断是否已处理（比批次日期更精确，避免同日重复入库）
                if last_fetched_at:
                    file_mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                    if file_mtime <= last_fetched_at:
                        continue

                item = RawItem(
                    source_id=self.source_id,
                    title=p.stem,  # 文件名去掉扩展名作为默认标题
                    raw_ref={"type": "file", "path": abs_path},
                    content_type=self.content_type,
                    raw_bytes=None,  # 文件已在磁盘，save_raw 因此跳过写入（no-op）
                    fetched_at=effective_at or captured_at or datetime.now(timezone.utc),
                    captured_at=captured_at,
                    effective_at=effective_at,
                )
                item._file_name = p.name  # save_raw 用此拼接路径，返回正确绝对路径
                items.append(item)

        return items

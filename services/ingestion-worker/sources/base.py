from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


def message_text(message) -> str:
    """Concatenate the text of all text blocks in an Anthropic response.

    Returns "" when the response has no content or no text block (tool-use /
    refusal / empty completion) instead of raising IndexError on content[0] or
    reading a non-text block.
    """
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


@dataclass
class RawItem:
    source_id: str
    title: str | None
    raw_ref: dict                    # {'type': 'file', 'path': '...'} | {'type': 'url', 'url': '...'}
    content_type: str                # 'text/html' | 'text/plain' | ...
    raw_bytes: bytes | None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    captured_at: datetime | None = None
    effective_at: datetime | None = None
    source_item_id: str | None = None
    document_instance_id: str | None = None   # Phase B: 稳定身份键


class BaseSource(ABC):
    fetch_mode: Literal["subscription", "one_shot"]

    @abstractmethod
    def fetch_new_items(self, last_fetched_at: datetime | None) -> list[RawItem]:
        """拉取自 last_fetched_at 以来的新内容。"""
        ...

    @abstractmethod
    def extract_text(self, raw: RawItem) -> str:
        """从 RawItem 中提取纯文本。"""
        ...

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class RawItem:
    source_id: str
    title: str | None
    raw_ref: dict                    # {'type': 'file', 'path': '...'} | {'type': 'url', 'url': '...'}
    content_type: str                # 'text/html' | 'text/plain' | ...
    raw_bytes: bytes | None
    fetched_at: datetime = field(default_factory=datetime.utcnow)


class BaseSource(ABC):
    fetch_mode: Literal["subscription", "one_shot", "push"]

    @abstractmethod
    def fetch_new_items(self, last_fetched_at: datetime | None) -> list[RawItem]:
        """拉取自 last_fetched_at 以来的新内容。push 型返回空列表。"""
        ...

    @abstractmethod
    def extract_text(self, raw: RawItem) -> str:
        """从 RawItem 中提取纯文本。"""
        ...

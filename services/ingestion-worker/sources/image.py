"""
Image Source — 使用 Claude Vision 生成图片内容的文字描述。
"""

import base64
import os
from pathlib import Path

import anthropic

from sources.base import RawItem
from sources.file_base import FileSourceMixin

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_claude = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])


class ImageSource(FileSourceMixin):
    content_type = "image"

    def __init__(self, source_id: str, uploads: list[dict]):
        self.source_id = source_id
        self.uploads = uploads

    def extract_text(self, raw: RawItem) -> str:
        path = raw.raw_ref["path"]
        p = Path(path)
        media_type = _MEDIA_TYPES.get(p.suffix.lower(), "image/jpeg")

        try:
            b64 = base64.standard_b64encode(p.read_bytes()).decode("utf-8")
            message = _claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "请详细描述这张图片的内容，输出中文。如果图片包含文字，请完整转录。",
                        },
                    ],
                }],
            )
            return message.content[0].text.strip()
        except Exception:
            return ""

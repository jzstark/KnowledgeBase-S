"""
Image Source — 使用 Claude Vision 生成图片内容的文字描述。

对于高度超过 7800px 的长图，自动切片后分段识别，避免缩放导致文字模糊。
OCR 完成后，调用第二次 Claude 对原始文字进行清洗（去除界面噪音、保留正文）。
"""

import base64
import io
import os
from pathlib import Path

import anthropic
from PIL import Image

import prompt_loader
from sources.base import RawItem
from sources.file_base import FileSourceMixin

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_DIM = 7800   # Claude 单边像素上限
TILE_H  = 7000   # 每个切片的高度
OVERLAP = 200    # 相邻切片的重叠像素，避免截断行内文字

_claude = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])


def _image_to_b64(img: Image.Image, media_type: str) -> str:
    buf = io.BytesIO()
    fmt = "JPEG" if "jpeg" in media_type else "PNG"
    img.save(buf, format=fmt, quality=92)
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def _call_claude(b64: str, media_type: str, tile_info: str = "") -> str:
    prompt = prompt_loader.get("image_ocr")
    if tile_info:
        prompt += f"（{tile_info}）"
    msg = _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return msg.content[0].text.strip()


def _cleanup(raw_text: str) -> str:
    """第二步：用 LLM 清洗 OCR 原文，去除界面噪音，保留正文。"""
    msg = _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"{prompt_loader.get('image_cleanup')}\n\n---\n\n{raw_text}",
        }],
    )
    return msg.content[0].text.strip()


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
            img = Image.open(p)
            w, h = img.size

            # 若宽度超限则等比缩小宽度（保留全高，后续按高度切片）
            if w > MAX_DIM:
                scale = MAX_DIM / w
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                w, h = img.size

            # 图片足够小，直接发送
            if h <= MAX_DIM:
                b64 = _image_to_b64(img, media_type)
                raw_text = _call_claude(b64, media_type)
            else:
                # 高图切片处理
                starts = list(range(0, h, TILE_H - OVERLAP))
                total = len(starts)
                parts = []
                for i, y0 in enumerate(starts):
                    y1 = min(y0 + TILE_H, h)
                    tile = img.crop((0, y0, w, y1))
                    b64 = _image_to_b64(tile, media_type)
                    text = _call_claude(b64, media_type, f"第{i+1}段，共{total}段")
                    parts.append(text)
                raw_text = "\n\n".join(parts)

            return _cleanup(raw_text)

        except Exception:
            return ""

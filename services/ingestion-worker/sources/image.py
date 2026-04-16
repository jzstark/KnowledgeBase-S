"""
Image Source — 使用 Claude Vision 进行 OCR，两步处理：
  1. _call_claude()：用 image_ocr 提示词转录图片文字
  2. _cleanup()：用 image_cleanup 提示词清洗界面噪音，保留正文

尺寸处理（参数见 config/image_processing.toml）：
  - 宽度超过 MAX_DIM 时等比缩小（安全兜底，极少触发）
  - 高度超过 MAX_DIM 时按 TILE_H 切片（含 OVERLAP 重叠），逐片识别后拼接
  - 每片/整图在发送前用 TILE_SCALE 等比缩放，降低 token 消耗
"""

import base64
import io
import logging
import os
import tomllib
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

_CFG_PATH = Path("/app/shared_config/image_processing.toml")
_cfg: dict = {}
if _CFG_PATH.exists():
    with open(_CFG_PATH, "rb") as _f:
        _cfg = tomllib.load(_f)

MAX_DIM    = int(_cfg.get("max_dim",    7800))
TILE_H     = int(_cfg.get("tile_h",    7000))
OVERLAP    = int(_cfg.get("overlap",    200))
TILE_SCALE = float(_cfg.get("tile_scale", 0.5))

_claude = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])


def _scale_for_api(img: Image.Image) -> Image.Image:
    """发送前等比缩放，降低 token 消耗。TILE_SCALE=1.0 时跳过。"""
    if TILE_SCALE >= 1.0:
        return img
    new_w = max(1, int(img.width * TILE_SCALE))
    new_h = max(1, int(img.height * TILE_SCALE))
    return img.resize((new_w, new_h), Image.LANCZOS)


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

            # 宽度超过硬上限时等比缩小（安全兜底，极少触发）
            if w > MAX_DIM:
                scale = MAX_DIM / w
                img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
                w, h = img.size

            # 图片足够小，直接发送
            if h <= MAX_DIM:
                b64 = _image_to_b64(_scale_for_api(img), media_type)
                raw_text = _call_claude(b64, media_type)
            else:
                # 高图切片处理
                starts = list(range(0, h, TILE_H - OVERLAP))
                total = len(starts)
                parts = []
                for i, y0 in enumerate(starts):
                    y1 = min(y0 + TILE_H, h)
                    tile = img.crop((0, y0, w, y1))
                    b64 = _image_to_b64(_scale_for_api(tile), media_type)
                    text = _call_claude(b64, media_type, f"第{i+1}段，共{total}段")
                    parts.append(text)
                raw_text = "\n\n".join(parts)

            return _cleanup(raw_text)

        except Exception:
            logging.getLogger(__name__).exception(f"[image] extract_text failed: {path}")
            return ""

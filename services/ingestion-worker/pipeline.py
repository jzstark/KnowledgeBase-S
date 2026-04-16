"""
Ingestion 流水线（所有 source 类型共用）：
  fetch → extract_text → save_raw → summarize → embed → POST /api/kb/ingest → update_last_fetched
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from openai import AsyncOpenAI

import prompt_loader
from sources.base import BaseSource, RawItem

logger = logging.getLogger(__name__)

API_BASE_URL = os.environ["API_BASE_URL"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

MAX_TEXT_CHARS = 12000   # ~4000 tokens


def save_raw(item: RawItem, source_type: str) -> str:
    """把原始内容写到 user_data/raw/{source_type}/，返回绝对路径。"""
    raw_dir = USER_DATA_DIR / USER_ID / "raw" / source_type
    raw_dir.mkdir(parents=True, exist_ok=True)

    file_name = getattr(item, "_file_name", None) or f"{datetime.utcnow().strftime('%Y-%m-%d')}-unknown.html"
    file_path = raw_dir / file_name

    if item.raw_bytes:
        file_path.write_bytes(item.raw_bytes)
    return str(file_path)


def summarize(text: str) -> tuple[str, list[str]]:
    """调用 Claude 生成摘要和标签，返回 (summary, tags)。"""
    truncated = text[:MAX_TEXT_CHARS]
    message = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": prompt_loader.fill("summarize", text=truncated),
            }
        ],
    )
    raw = message.content[0].text.strip()
    # 提取 JSON（Claude 有时会在 ```json ``` 里）
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # 先尝试严格解析
    try:
        data = json.loads(raw)
        return data["summary"], data.get("tags", [])
    except json.JSONDecodeError:
        pass

    # 降级：用正则分别提取 summary 和 tags，容忍摘要中含有未转义引号的情况
    summary = ""
    tags: list[str] = []
    m = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if m:
        summary = m.group(1)
    m = re.search(r'"tags"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
    if m:
        tags = re.findall(r'"([^"]*)"', m.group(1))
    return summary, tags


async def embed(text: str) -> list[float]:
    """调用 OpenAI text-embedding-3-small 生成 1536 维向量。"""
    resp = await openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],
        dimensions=1536,
    )
    return resp.data[0].embedding


async def post_ingest(payload: dict) -> str:
    """POST /api/kb/ingest，返回 node_id。"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{API_BASE_URL}/api/kb/ingest", json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()["id"]


async def update_last_fetched(source_id: str):
    now = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient() as client:
        await client.put(
            f"{API_BASE_URL}/api/sources/{source_id}",
            json={"last_fetched_at": now},
            timeout=10,
        )


async def run_pipeline(source: BaseSource, source_config: dict):
    source_id = source_config["id"]
    source_type = source_config["type"]

    last_fetched_at = source_config.get("last_fetched_at")
    if last_fetched_at:
        if isinstance(last_fetched_at, str):
            last_fetched_at = datetime.fromisoformat(last_fetched_at.replace("Z", "+00:00"))

    logger.info(f"[{source_id}] 开始抓取，last_fetched_at={last_fetched_at}")
    items: list[RawItem] = source.fetch_new_items(last_fetched_at)
    logger.info(f"[{source_id}] 获取到 {len(items)} 条新内容")

    for item in items:
        try:
            # 1. 提取正文
            text = source.extract_text(item)
            if not text or len(text) < 50:
                logger.warning(f"[{source_id}] 跳过，正文过短: {item.title}")
                continue

            # 2. 保存原始文件
            file_path = save_raw(item, source_type)
            if item.raw_ref.get("type") == "url":
                raw_ref = {"type": "url", "url": item.raw_ref["url"], "cached": file_path}
            else:
                raw_ref = {"type": "file", "path": file_path}

            # 3. 摘要 + 标签（同步调用）
            summary, tags = summarize(text)
            logger.info(f"[{source_id}] 摘要完成: {item.title}")

            # 4. Embedding
            embedding = await embed(summary)

            # 5. 入库
            node_id = await post_ingest({
                "user_id": USER_ID,
                "title": item.title,
                "summary": summary,
                "embedding": embedding,
                "source_type": source_type,
                "source_id": source_id,
                "raw_ref": raw_ref,
                "tags": tags,
                "is_primary": source_config.get("is_primary", True),
            })
            logger.info(f"[{source_id}] 入库成功: {node_id} — {item.title}")

            # 6. 生成 wiki/nodes/ md 文件（正文为全量清洗后原文，非摘要）
            write_wiki_node(node_id, item, text, tags, raw_ref)

        except Exception as e:
            logger.error(f"[{source_id}] 处理失败: {item.title} — {e}", exc_info=True)

    await update_last_fetched(source_id)
    logger.info(f"[{source_id}] 完成，已更新 last_fetched_at")


def write_wiki_node(node_id: str, item: RawItem, text: str, tags: list[str], raw_ref: dict):
    """生成 wiki/nodes/{node_id}.md，兼容 Obsidian。正文为全量清洗后原文，非摘要。"""
    wiki_dir = USER_DATA_DIR / USER_ID / "wiki" / "nodes"
    wiki_dir.mkdir(parents=True, exist_ok=True)

    raw_ref_path = raw_ref.get("path") or raw_ref.get("url", "")
    tags_yaml = "[" + ", ".join(tags) + "]"
    created = item.fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    content = f"""---
id: {node_id}
source_type: {item.raw_ref.get("type", "unknown")}
raw_ref: {raw_ref_path}
tags: {tags_yaml}
created_at: {created}
---

# {item.title or "（无标题）"}

{text}
"""
    (wiki_dir / f"{node_id}.md").write_text(content, encoding="utf-8")

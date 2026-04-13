"""
Ingestion Worker 入口。

运行模式：
  python main.py          # 循环模式：每小时轮询 + HTTP trigger server（端口 8001）
  python main.py --once   # 单次模式：立即执行一次后退出（手动触发/调试）
"""

import asyncio
import logging
import os
import sys

import httpx
import uvicorn
from fastapi import FastAPI

from pipeline import run_pipeline
from sources.image import ImageSource
from sources.pdf import PDFSource
from sources.plaintext import PlaintextSource
from sources.rss import RSSSource
from sources.url import URLSource
from sources.word import WordSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

API_BASE_URL = os.environ["API_BASE_URL"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", 3600))


# ── HTTP Trigger Server ───────────────────────────────────────────────────────

trigger_app = FastAPI(title="Ingestion Trigger")


@trigger_app.post("/trigger/{source_id}")
async def trigger_one(source_id: str):
    """触发单个 source 的抓取（立即返回，后台运行）。"""
    sources = await fetch_sources()
    config = next((s for s in sources if s["id"] == source_id), None)
    if not config:
        return {"ok": False, "detail": "source not found"}
    source = build_source(config)
    if source is None:
        return {"ok": False, "detail": f"unsupported type: {config.get('type')}"}
    asyncio.create_task(run_pipeline(source, config))
    return {"ok": True}


@trigger_app.post("/trigger")
async def trigger_all():
    """触发所有订阅源（相当于 --once，后台运行）。"""
    asyncio.create_task(run_once())
    return {"ok": True}


# ── Core Logic ────────────────────────────────────────────────────────────────

async def fetch_sources() -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_BASE_URL}/api/sources", timeout=10)
        resp.raise_for_status()
        return resp.json()


def build_source(config: dict):
    """根据 source 配置构造对应的 Source 实例。"""
    t = config["type"]
    raw_config = config.get("config") or {}
    if isinstance(raw_config, str):
        import json
        raw_config = json.loads(raw_config)

    if t == "rss":
        url = raw_config.get("url", "")
        return RSSSource(source_id=config["id"], feed_url=url)
    elif t == "url":
        url = raw_config.get("url", "")
        return URLSource(source_id=config["id"], url=url)
    elif t in ("pdf", "image", "plaintext", "word"):
        uploads = raw_config.get("uploads", [])
        cls = {"pdf": PDFSource, "image": ImageSource,
               "plaintext": PlaintextSource, "word": WordSource}[t]
        return cls(source_id=config["id"], uploads=uploads)
    return None


async def run_once():
    """轮询模式下只处理自动抓取型（subscription）sources；
    manual/push 型由用户通过 trigger 端点显式触发。"""
    sources = await fetch_sources()
    subscription_sources = [s for s in sources if s.get("fetch_mode") == "subscription"]
    logger.info(f"共 {len(subscription_sources)} 个订阅源待自动抓取")

    for config in subscription_sources:
        source = build_source(config)
        if source is None:
            logger.info(f"跳过暂未支持的类型: {config['type']}")
            continue
        await run_pipeline(source, config)


async def poll_loop():
    logger.info(f"循环模式启动，轮询间隔 {POLL_INTERVAL}s")
    while True:
        try:
            await run_once()
        except Exception as e:
            logger.error(f"本轮运行异常: {e}", exc_info=True)
        logger.info(f"等待 {POLL_INTERVAL}s 后下次轮询...")
        await asyncio.sleep(POLL_INTERVAL)


async def main():
    once = "--once" in sys.argv
    if once:
        logger.info("单次模式启动")
        await run_once()
        logger.info("单次运行完成")
    else:
        server = uvicorn.Server(
            uvicorn.Config(trigger_app, host="0.0.0.0", port=8001, log_level="warning")
        )
        await asyncio.gather(server.serve(), poll_loop())


if __name__ == "__main__":
    asyncio.run(main())

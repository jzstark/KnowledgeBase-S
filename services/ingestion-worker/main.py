"""
Ingestion Worker 入口。

运行模式：
  python main.py          # 循环模式：每小时轮询一次（docker 长期运行）
  python main.py --once   # 单次模式：立即执行一次后退出（手动触发/调试）
"""

import asyncio
import logging
import os
import sys

import httpx

from pipeline import run_pipeline
from sources.rss import RSSSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

API_BASE_URL = os.environ["API_BASE_URL"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", 3600))


async def fetch_sources() -> list[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_BASE_URL}/api/sources", timeout=10)
        resp.raise_for_status()
        return resp.json()


def build_source(config: dict):
    """根据 source 配置构造对应的 Source 实例。"""
    t = config["type"]
    # JSONB 字段从数据库取出可能是字符串，需要反序列化
    raw_config = config.get("config") or {}
    if isinstance(raw_config, str):
        import json
        raw_config = json.loads(raw_config)
    if t == "rss":
        url = raw_config.get("url", "")
        return RSSSource(source_id=config["id"], feed_url=url)
    # 其他类型后续步骤实现
    return None


async def run_once():
    sources = await fetch_sources()
    subscription_sources = [s for s in sources if s.get("fetch_mode") == "subscription"]
    logger.info(f"共 {len(subscription_sources)} 个订阅源待处理")

    for config in subscription_sources:
        source = build_source(config)
        if source is None:
            logger.info(f"跳过暂未支持的类型: {config['type']}")
            continue
        await run_pipeline(source, config)


async def main():
    once = "--once" in sys.argv
    if once:
        logger.info("单次模式启动")
        await run_once()
        logger.info("单次运行完成")
    else:
        logger.info(f"循环模式启动，轮询间隔 {POLL_INTERVAL}s")
        while True:
            try:
                await run_once()
            except Exception as e:
                logger.error(f"本轮运行异常: {e}", exc_info=True)
            logger.info(f"等待 {POLL_INTERVAL}s 后下次轮询...")
            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())

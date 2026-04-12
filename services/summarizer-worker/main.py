"""
Summarizer Worker — 每日简报生成器。

实际分类逻辑已内嵌在 api/routers/briefing.py（POST /api/briefing/generate）。
本 worker 负责定时触发该端点，或在 --once 模式下立即触发一次。

运行模式：
  python main.py          # 按 briefing_time 定时触发
  python main.py --once   # 立即触发一次后退出
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

API_BASE_URL = os.environ["API_BASE_URL"]
API_PASSWORD = os.environ["AUTH_PASSWORD"]


async def get_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/auth/login",
            json={"password": API_PASSWORD},
            timeout=10,
        )
        resp.raise_for_status()
        cookie = resp.cookies.get("token")
        if not cookie:
            raise RuntimeError("登录未返回 token cookie")
        return cookie


async def trigger_generate(token: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{API_BASE_URL}/api/briefing/generate",
            cookies={"token": token},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        total = sum(len(g["nodes"]) for g in data.get("groups", []))
        logger.info(f"简报生成完成：{len(data.get('groups', []))} 个分组，共 {total} 篇")
        return data


async def run_once():
    logger.info("开始生成今日简报...")
    token = await get_token()
    await trigger_generate(token)


async def main():
    once = "--once" in sys.argv
    if once:
        await run_once()
        return

    logger.info("定时模式启动，将在每日 briefing_time 触发")
    while True:
        now = datetime.now(timezone.utc)
        # 简单实现：每小时检查一次，整点触发（实际部署由 scheduler 精确控制）
        logger.info(f"等待下次触发... 当前 UTC: {now.strftime('%H:%M')}")
        await asyncio.sleep(3600)
        try:
            await run_once()
        except Exception as e:
            logger.error(f"生成失败: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())

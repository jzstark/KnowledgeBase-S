"""
Feedback Worker

POST /analyze  — 接收草稿 diff，调用 Claude 提炼偏好规则，写入 writing_memory
"""

import difflib
import json
import logging
import os
import re

import anthropic
import httpx
from fastapi import FastAPI
from pydantic import BaseModel

import prompt_loader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Feedback Worker")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000")
claude = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))


class AnalyzeRequest(BaseModel):
    draft_id: str
    draft_content: str
    final_content: str
    template_name: str = "default"


@app.post("/analyze")
async def analyze_feedback(body: AnalyzeRequest):
    """Diff 分析 + Claude → 偏好规则 → 写 /api/kb/memory/feedback"""

    # 1. 用 difflib 计算统一 diff
    diff_lines = list(difflib.unified_diff(
        body.draft_content.splitlines(),
        body.final_content.splitlines(),
        lineterm="",
        n=2,
    ))
    if not diff_lines:
        logger.info(f"draft {body.draft_id}: 无差异，跳过分析")
        return {"rules_extracted": 0}

    diff_text = "\n".join(diff_lines[:200])  # 截断防超 token

    # 2. Claude 分析 diff → JSON 规则
    prompt = prompt_loader.fill("feedback_analysis", diff_text=diff_text)

    try:
        message = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        logger.info(f"draft {body.draft_id}: Claude 返回: {raw[:200]}")
    except Exception as e:
        logger.error(f"draft {body.draft_id}: Claude 调用失败: {e}")
        return {"rules_extracted": 0}

    # 3. 解析 JSON — 提取 [...] 块防止模型多输出文字
    try:
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        rules: list[dict] = json.loads(m.group()) if m else []
    except Exception as e:
        logger.warning(f"draft {body.draft_id}: JSON 解析失败: {e}, raw={raw[:200]}")
        rules = []

    # 4. 逐条写入 writing_memory（通过 API）
    saved = 0
    async with httpx.AsyncClient() as client:
        for r in rules:
            rule_text = r.get("rule", "").strip()
            if not rule_text:
                continue
            try:
                resp = await client.post(
                    f"{API_BASE_URL}/api/kb/memory/feedback",
                    json={
                        "template_name": body.template_name,
                        "rule": rule_text,
                        "rule_type": r.get("rule_type", "style"),
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                saved += 1
            except Exception as e:
                logger.warning(f"写入规则失败: {e}")

    logger.info(f"draft {body.draft_id}: 提炼并写入 {saved} 条偏好规则")
    return {"rules_extracted": saved}


@app.get("/health")
def health():
    return {"ok": True}

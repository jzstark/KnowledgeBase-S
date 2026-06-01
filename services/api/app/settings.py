import shutil
import tempfile
import zipfile
from pathlib import Path
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])

USER_ID = "default"
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))

SCHEMA_DEFAULT = """\
# 知识库宪法（Schema）

> ⚠️ 此文件定义系统对内容的理解和处理方式。修改后只影响新入库内容，不影响已有节点。
> 建议在理解各字段含义后再修改，避免摘要质量下降；若内容缺失，系统将使用内置默认行为。

## 知识分类体系

本知识库使用以下标签体系（每个节点可有多个标签）：

- `AI`：大模型、AI 应用、相关研究
- `产品`：产品设计、用户体验、功能规划
- `商业`：市场分析、商业模式、融资动态
- `技术`：软件工程、基础设施、开发工具
- `创业`：创业经验、团队管理、增长策略
- `行业`：行业趋势、竞争格局、政策监管

## 摘要生成规范

- **长度**：3~5 句话，不超过 200 字
- **视角**：从读者角度提炼对决策或理解有价值的核心观点
- **语言**：中文，简洁直接，避免堆砌原文
- **重点**：「是什么」+「为什么重要」，包含作者的关键结论

## 内容准入标准

**建议入库**：有明确观点或结论的深度文章、对决策有参考价值的案例、自己的笔记和草稿

**可以跳过**：纯新闻快讯（无观点）、营销软文、同一事件的多篇重复报道

## 领域词汇与背景

（在此填写你的领域背景、专有名词解释、行业黑话，帮助 AI 更准确理解你的内容）
"""


@router.get("/schema")
async def get_schema(_: dict = Depends(require_auth)):
    p = USER_DATA_DIR / USER_ID / "config" / "schema.md"
    content = p.read_text(encoding="utf-8") if p.exists() else SCHEMA_DEFAULT
    return {"content": content}


class SchemaSave(BaseModel):
    content: str


@router.put("/schema")
async def save_schema(body: SchemaSave, _: dict = Depends(require_auth)):
    p = USER_DATA_DIR / USER_ID / "config" / "schema.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True}


@router.get("/export")
async def export_user_data(_: dict = Depends(require_auth)):
    user_dir = USER_DATA_DIR / USER_ID
    if not user_dir.exists():
        raise HTTPException(404, "暂无用户数据")
    tmp = tempfile.mktemp(suffix=".zip")
    shutil.make_archive(tmp[:-4], "zip", user_dir.parent, USER_ID)
    return FileResponse(tmp, media_type="application/zip", filename="knowledgebase-export.zip")


@router.get("/export/no-raw")
async def export_user_data_no_raw(_: dict = Depends(require_auth)):
    user_dir = USER_DATA_DIR / USER_ID
    if not user_dir.exists():
        raise HTTPException(404, "暂无用户数据")
    tmp = tempfile.mktemp(suffix=".zip")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in user_dir.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(user_dir)
            if rel.parts[0] == "raw":
                continue
            zf.write(f, Path(USER_ID) / rel)
    return FileResponse(tmp, media_type="application/zip", filename="knowledgebase-export-no-raw.zip")

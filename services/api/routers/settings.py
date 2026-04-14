import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])

USER_ID = "default"
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))

DEFAULT_SETTINGS = {
    "topics": "科技行业动态、AI 前沿、产品设计",
    "briefing_hours_back": 24,
    "briefing_time": "08:00",
    "maintenance_frequency": "weekly",
}

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

## 关系识别规则

- `similar_to`：主题相近（系统自动建立，无需手动干预）
- `extends`：A 节点是对 B 节点观点的延伸或深化
- `background_of`：A 节点为理解 B 节点提供背景知识
- `contradicts`：A 节点与 B 节点持相反观点
- `supports`：A 节点提供了支持 B 节点的证据或案例

## 内容准入标准

**建议入库**：有明确观点或结论的深度文章、对决策有参考价值的案例、自己的笔记和草稿

**可以跳过**：纯新闻快讯（无观点）、营销软文、同一事件的多篇重复报道

## 领域词汇与背景

（在此填写你的领域背景、专有名词解释、行业黑话，帮助 AI 更准确理解你的内容）
"""


async def get_settings_dict() -> dict:
    row = await database.database.fetch_one(
        "SELECT settings FROM user_settings WHERE user_id = :user_id",
        {"user_id": USER_ID},
    )
    if not row:
        result = DEFAULT_SETTINGS.copy()
    else:
        raw = row["settings"]
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        result = {**DEFAULT_SETTINGS, **data}

    # topics 文件优先于 DB（向后兼容：文件不存在时使用 DB 值）
    topics_file = USER_DATA_DIR / USER_ID / "config" / "topics.md"
    if topics_file.exists():
        result["topics"] = topics_file.read_text(encoding="utf-8").strip()

    return result


class SettingsUpdate(BaseModel):
    briefing_hours_back: int | None = None
    briefing_time: str | None = None
    maintenance_frequency: str | None = None


@router.get("")
async def get_settings(_: dict = Depends(require_auth)):
    return await get_settings_dict()


@router.put("")
async def update_settings(body: SettingsUpdate, _: dict = Depends(require_auth)):
    current = await get_settings_dict()
    updates = body.model_dump(exclude_none=True)
    merged = {**current, **updates}

    await database.database.execute(
        """
        INSERT INTO user_settings (user_id, settings)
        VALUES (:user_id, :settings)
        ON CONFLICT (user_id) DO UPDATE SET settings = :settings
        """,
        {"user_id": USER_ID, "settings": database.jsonb(merged)},
    )
    return merged


# ── 选题方向（文件存储）────────────────────────────────────────────────────────

@router.get("/topics")
async def get_topics(_: dict = Depends(require_auth)):
    """读取选题方向；若文件不存在则返回 DB 中的值。"""
    p = USER_DATA_DIR / USER_ID / "config" / "topics.md"
    if p.exists():
        return {"content": p.read_text(encoding="utf-8")}
    settings = await get_settings_dict()
    return {"content": settings.get("topics", "")}


class TopicsSave(BaseModel):
    content: str


@router.put("/topics")
async def save_topics(body: TopicsSave, _: dict = Depends(require_auth)):
    """保存选题方向到 config/topics.md。"""
    p = USER_DATA_DIR / USER_ID / "config" / "topics.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True}


# ── 知识库宪法（Schema）────────────────────────────────────────────────────────

@router.get("/schema")
async def get_schema(_: dict = Depends(require_auth)):
    """读取 schema.md；若不存在则返回默认内容（不自动写入文件）。"""
    p = USER_DATA_DIR / USER_ID / "config" / "schema.md"
    content = p.read_text(encoding="utf-8") if p.exists() else SCHEMA_DEFAULT
    return {"content": content}


class SchemaSave(BaseModel):
    content: str


@router.put("/schema")
async def save_schema(body: SchemaSave, _: dict = Depends(require_auth)):
    """保存 schema.md。内容为纯文本，系统不解析不执行。"""
    p = USER_DATA_DIR / USER_ID / "config" / "schema.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body.content, encoding="utf-8")
    return {"ok": True}


# ── 模板 CRUD ──────────────────────────────────────────────────────────────────

def _template_dir() -> Path:
    return USER_DATA_DIR / USER_ID / "config" / "templates"


@router.get("/templates")
async def list_templates(_: dict = Depends(require_auth)):
    """列出所有模板名称。"""
    d = _template_dir()
    if not d.exists():
        return []
    names = [p.stem for p in d.glob("*.md")] + [p.stem for p in d.glob("*.txt")]
    return sorted(set(names))


@router.get("/templates/{name}")
async def get_template(name: str, _: dict = Depends(require_auth)):
    """读取单个模板内容。"""
    d = _template_dir()
    for ext in (".md", ".txt"):
        p = d / f"{name}{ext}"
        if p.exists():
            return {"name": name, "content": p.read_text(encoding="utf-8")}
    raise HTTPException(404, "模板不存在")


class TemplateSave(BaseModel):
    content: str


@router.put("/templates/{name}")
async def save_template(name: str, body: TemplateSave, _: dict = Depends(require_auth)):
    """保存（新建或更新）模板。名称只允许字母/数字/下划线/中文/连字符。"""
    if not re.match(r"^[\w\u4e00-\u9fff\-]+$", name):
        raise HTTPException(400, "模板名称不合法")
    d = _template_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(body.content, encoding="utf-8")
    return {"ok": True}


@router.delete("/templates/{name}")
async def delete_template(name: str, _: dict = Depends(require_auth)):
    """删除模板文件。"""
    d = _template_dir()
    for ext in (".md", ".txt"):
        p = d / f"{name}{ext}"
        if p.exists():
            p.unlink()
            return {"ok": True}
    raise HTTPException(404, "模板不存在")


# ── 数据导出 ───────────────────────────────────────────────────────────────────

@router.get("/export")
async def export_user_data(_: dict = Depends(require_auth)):
    """打包 user_data/{user_id}/ 为 zip 文件供下载。"""
    user_dir = USER_DATA_DIR / USER_ID
    if not user_dir.exists():
        raise HTTPException(404, "暂无用户数据")
    tmp = tempfile.mktemp(suffix=".zip")
    shutil.make_archive(tmp[:-4], "zip", user_dir.parent, USER_ID)
    return FileResponse(
        tmp,
        media_type="application/zip",
        filename="knowledgebase-export.zip",
    )

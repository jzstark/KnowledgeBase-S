"""
文件资源管理 API。

提供对 user_data 目录下三个区域的访问：
  - raw/   原始上传文件（只读列表 + 删除通过 kb.py 的节点删除接口）
  - wiki/  自动生成的 Markdown 笔记（可读写）
  - config/ 用户配置模板（可读写）
"""

import os
import pathlib

import database
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

router = APIRouter(prefix="/api/files", tags=["files"])

RAW_TYPES = ["pdf", "image", "wechat", "plaintext", "word"]
EDITABLE_PREFIXES = ("wiki/", "config/")


def _user_dir() -> pathlib.Path:
    return USER_DATA_DIR / USER_ID


def _safe_editable(rel_path: str) -> pathlib.Path:
    """
    Resolve rel_path within the user directory and verify it stays inside
    an editable area (wiki/ or config/). Raises 403 otherwise.
    """
    if not any(rel_path.startswith(p) for p in EDITABLE_PREFIXES):
        raise HTTPException(status_code=403, detail="该路径不可编辑")
    base = _user_dir()
    resolved = (base / rel_path).resolve()
    # Guard against path traversal
    if not str(resolved).startswith(str(base.resolve())):
        raise HTTPException(status_code=403, detail="路径不合法")
    return resolved


# ── 目录树 ────────────────────────────────────────────────────────────────────

@router.get("/tree")
async def get_tree():
    """返回 user_data 目录树（raw / wiki / config 三区）。"""
    base = _user_dir()

    # ── raw 区：按 source type 分组，每个文件关联 node_id ──
    raw: dict[str, list[dict]] = {t: [] for t in RAW_TYPES}
    raw_dir = base / "raw"
    if raw_dir.exists():
        # 批量查出所有 raw_ref path → node_id 的映射
        rows = await database.database.fetch_all(
            "SELECT id, raw_ref->>'path' AS path FROM knowledge_nodes WHERE user_id = :uid",
            {"uid": USER_ID},
        )
        path_to_node: dict[str, str] = {r["path"]: r["id"] for r in rows if r["path"]}

        for type_name in RAW_TYPES:
            type_dir = raw_dir / type_name
            if not type_dir.exists():
                continue
            for f in sorted(type_dir.iterdir()):
                if not f.is_file():
                    continue
                abs_str = str(f)
                raw[type_name].append({
                    "name": f.name,
                    "rel_path": f"raw/{type_name}/{f.name}",
                    "size": f.stat().st_size,
                    "node_id": path_to_node.get(abs_str),
                })

    # ── wiki 区：nodes/*.md + index.md ──
    wiki: list[dict] = []
    wiki_dir = base / "wiki"
    if wiki_dir.exists():
        index = wiki_dir / "index.md"
        if index.exists():
            wiki.append({"name": "index.md", "rel_path": "wiki/index.md"})
        nodes_dir = wiki_dir / "nodes"
        if nodes_dir.exists():
            for f in sorted(nodes_dir.iterdir()):
                if f.is_file() and f.suffix == ".md":
                    wiki.append({"name": f.name, "rel_path": f"wiki/nodes/{f.name}"})

    # ── config 区：templates/*.md ──
    config: list[dict] = []
    config_dir = base / "config" / "templates"
    if config_dir.exists():
        for f in sorted(config_dir.iterdir()):
            if f.is_file() and f.suffix == ".md":
                config.append({"name": f.name, "rel_path": f"config/templates/{f.name}"})

    return {"raw": raw, "wiki": wiki, "config": config}


# ── 文件内容读写 ───────────────────────────────────────────────────────────────

@router.get("/content")
async def get_content(rel_path: str = Query(...)):
    """读取 wiki/ 或 config/ 下的 Markdown 文件内容。"""
    path = _safe_editable(rel_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"content": path.read_text(encoding="utf-8")}


class WriteContentBody(BaseModel):
    rel_path: str
    content: str


@router.put("/content")
async def put_content(body: WriteContentBody):
    """写入 wiki/ 或 config/ 下的 Markdown 文件内容。"""
    path = _safe_editable(body.rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.content, encoding="utf-8")
    return {"ok": True}

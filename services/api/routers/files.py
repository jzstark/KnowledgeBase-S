"""
文件资源管理 API。

提供对 user_data 目录下两个区域的访问：
  - raw/   原始上传文件（只读列表 + 删除通过 kb.py 的节点删除接口）
  - wiki/  自动生成的 Markdown 笔记（只读导出）
"""

import os
import pathlib

import database
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_auth

USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
USER_ID = "default"

router = APIRouter(prefix="/api/files", tags=["files"], dependencies=[Depends(require_auth)])

RAW_TYPES = ["pdf", "image", "wechat", "plaintext", "word"]
READABLE_PREFIXES = ("wiki/",)


def _user_dir() -> pathlib.Path:
    return USER_DATA_DIR / USER_ID


def _safe_path(rel_path: str, allowed_prefixes: tuple[str, ...], deny_detail: str) -> pathlib.Path:
    """
    Resolve rel_path within the user directory and verify it stays inside
    an allowed area. Raises 403 otherwise.

    The allowed-area check runs against the *normalized* path (after resolving
    any `..`).
    """
    base = _user_dir().resolve()
    resolved = (base / rel_path).resolve()
    # Guard against path traversal escaping the user directory
    if not resolved.is_relative_to(base):
        raise HTTPException(status_code=403, detail="路径不合法")
    rel = resolved.relative_to(base).as_posix()
    if not any(rel == p.rstrip("/") or rel.startswith(p) for p in allowed_prefixes):
        raise HTTPException(status_code=403, detail=deny_detail)
    return resolved


def _safe_readable(rel_path: str) -> pathlib.Path:
    return _safe_path(rel_path, READABLE_PREFIXES, "该路径不可读取")


# ── 目录树 ────────────────────────────────────────────────────────────────────

@router.get("/tree")
async def get_tree():
    """返回 user_data 目录树（raw / wiki 两区）。"""
    base = _user_dir()

    # ── raw 区：按 source type 分组，每个文件关联 node_id ──
    raw: dict[str, list[dict]] = {t: [] for t in RAW_TYPES}
    raw_dir = base / "raw"
    if raw_dir.exists():
        rows = await database.database.fetch_all(
            """
            SELECT n.id, ra.storage_key AS path
            FROM knowledge_nodes n
            JOIN article_nodes an ON an.node_id = n.id
            JOIN document_instances di ON di.id = an.document_instance_id
            JOIN raw_assets ra ON ra.id = di.raw_asset_id
            WHERE n.user_id = :uid
            """,
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

    # ── wiki 区：articles/ entities/ summaries/ indices/ + index.md ──
    # Format: {"articles": [...], "entities": [...], "summaries": [...], "indices": [...], "index": bool}
    wiki: dict = {"articles": [], "entities": [], "summaries": [], "indices": [], "index": False}
    wiki_dir = base / "wiki"
    if wiki_dir.exists():
        if (wiki_dir / "index.md").exists():
            wiki["index"] = True
        for subdir in ("articles", "entities", "summaries", "indices"):
            sd = wiki_dir / subdir
            if sd.exists():
                for f in sorted(sd.iterdir()):
                    if f.is_file() and f.suffix == ".md":
                        wiki[subdir].append({"name": f.name, "rel_path": f"wiki/{subdir}/{f.name}"})

    return {"raw": raw, "wiki": wiki}


# ── 文件内容读取 ───────────────────────────────────────────────────────────────

@router.get("/content")
async def get_content(rel_path: str = Query(...)):
    """读取 wiki/ 下的 Markdown 文件内容。"""
    path = _safe_readable(rel_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"content": path.read_text(encoding="utf-8")}

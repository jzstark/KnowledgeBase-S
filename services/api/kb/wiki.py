import json

import database
from kb.graph import fetch_node_with_object_fields
from kb.common import USER_DATA_DIR, is_visible_edge


def wiki_subdir(object_type: str) -> str:
    return {
        "article": "articles",
        "entity": "entities",
        "summary": "summaries",
        "index": "indices",
    }.get(object_type, "articles")


def wiki_file_path(user_id: str, node_id: str, object_type: str):
    subdir = wiki_subdir(object_type)
    return USER_DATA_DIR / user_id / "wiki" / subdir / f"{node_id}.md"


def read_wiki_body(user_id: str, node_id: str, object_type: str, limit: int | None = 4000) -> str:
    path = wiki_file_path(user_id, node_id, object_type)
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    parts = raw.split("---", 2)
    body = parts[2].strip() if len(parts) >= 3 else raw.strip()
    for marker in ("\n## 关联节点\n", "\n## 関連節点\n"):
        if marker in body:
            body = body[: body.index(marker)].strip()
    lines = body.split("\n", 2)
    if len(lines) >= 3:
        body = lines[2].strip()
    if limit is None:
        return body
    return body[:limit] + ("..." if len(body) > limit else "")


async def write_wiki_node(node_id: str, user_id: str) -> None:
    """Write one knowledge node to its wiki markdown file."""
    node = await fetch_node_with_object_fields(node_id)
    if not node:
        return

    object_type = node.get("object_type") or "article"
    edges = await database.database.fetch_all(
        "SELECT from_node_id, to_node_id, relation_type FROM knowledge_edges "
        "WHERE from_node_id = :id OR to_node_id = :id",
        {"id": node_id},
    )

    tags: list[str] = list(node["tags"]) if node["tags"] else []
    created_at = node["created_at"].isoformat() if node["created_at"] else ""
    updated_at = node["updated_at"].isoformat() if node.get("updated_at") else created_at
    ingested_at = node["ingested_at"].isoformat() if node.get("ingested_at") else ""
    published_at = node["published_at"].isoformat() if node.get("published_at") else ""
    source_published_at = node["source_published_at"].isoformat() if node.get("source_published_at") else ""
    source_updated_at = node["source_updated_at"].isoformat() if node.get("source_updated_at") else ""
    captured_at = node["captured_at"].isoformat() if node.get("captured_at") else ""
    effective_at = node["effective_at"].isoformat() if node.get("effective_at") else ""

    raw_ref = node.get("raw_ref") or {}
    if isinstance(raw_ref, str):
        raw_ref = json.loads(raw_ref)
    raw_ref_str = ""
    if raw_ref:
        if raw_ref.get("type") == "file":
            raw_ref_str = raw_ref.get("path", "")
        elif raw_ref.get("type") == "url":
            raw_ref_str = raw_ref.get("url", "")

    wikilinks = []
    relations = []
    for e in edges:
        ed = dict(e)
        other = ed["to_node_id"] if ed["from_node_id"] == node_id else ed["from_node_id"]
        if not is_visible_edge(ed["relation_type"]):
            continue
        if ed["relation_type"] in ("wikilink", "mentions"):
            wikilinks.append(other)
        else:
            relations.append({"id": other, "type": ed["relation_type"]})

    tags_yaml = "[" + ", ".join(tags) + "]"
    wikilinks_yaml = "[" + ", ".join(wikilinks) + "]"
    source_node_ids = list(node.get("source_node_ids") or [])
    sources_yaml = "[" + ", ".join(source_node_ids) + "]"
    aliases = list(node.get("aliases") or [])
    aliases_yaml = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"

    relations_yaml = ""
    if relations:
        relations_yaml = "\nrelations:"
        for rel in relations:
            relations_yaml += f"\n  - id: {rel['id']}\n    type: {rel['type']}"

    title = node["title"] or node_id
    wiki_file = wiki_file_path(user_id, node_id, object_type)
    wiki_file.parent.mkdir(parents=True, exist_ok=True)

    export_body = node["abstract"] or ""
    perspective_val = node.get("perspective") or ""
    perspective_label = node.get("perspective_label") or perspective_val
    perspective_instruction = node.get("perspective_instruction") or perspective_val
    is_default = "true" if node.get("is_default") else "false"
    extra_fm = f"\nsource_type: {node.get('source_type') or ''}\nstorage_key: {raw_ref_str}"
    if object_type == "entity":
        extra_fm = f"\ncanonical_name: {node.get('canonical_name') or title}\naliases: {aliases_yaml}\nsources: {sources_yaml}"
    elif object_type == "summary":
        extra_fm = (
            f"\nsummary_of: {node.get('summary_of') or ''}\nsources: {sources_yaml}"
            f"\nperspective: {perspective_val}\nperspective_label: {perspective_label}"
            f"\nperspective_instruction: {perspective_instruction}\nis_default: {is_default}"
        )
    elif object_type == "index":
        extra_fm = f"\nsource_type: {node.get('source_type') or ''}\nstorage_key: {raw_ref_str}\nperspective: {perspective_val}"

    content = f"""---
id: {node_id}
type: {object_type}
title: "{title}"
tags: {tags_yaml}
wikilinks: {wikilinks_yaml}{extra_fm}
created_at: {created_at}
ingested_at: {ingested_at}
published_at: {published_at}
source_published_at: {source_published_at}
source_updated_at: {source_updated_at}
captured_at: {captured_at}
effective_at: {effective_at}
updated_at: {updated_at}{relations_yaml}
---

# {title}

{export_body}
"""
    if relations:
        content += "\n## 关联节点\n"
        for rel in relations:
            content += f"- [[{rel['id']}]] · {rel['type']}\n"

    wiki_file.write_text(content, encoding="utf-8")


async def write_wiki_index(user_id: str) -> None:
    rows = await database.database.fetch_all(
        """
        SELECT id, title, tags, object_type, created_at
        FROM knowledge_nodes WHERE user_id = :user_id
        ORDER BY object_type, created_at DESC
        """,
        {"user_id": user_id},
    )

    lines = ["# 知识库索引\n\n> 自动生成，请勿手动修改。\n\n"]
    lines.append(f"共 **{len(rows)}** 个对象。\n")

    sections: dict[str, list] = {"article": [], "entity": [], "summary": [], "index": []}
    for r in rows:
        r = dict(r)
        ot = r.get("object_type") or "article"
        sections.setdefault(ot, []).append(r)

    section_labels = {"article": "文章", "entity": "实体", "summary": "摘要", "index": "目录"}
    for ot, items in sections.items():
        if not items:
            continue
        label = section_labels.get(ot, ot)
        subdir = wiki_subdir(ot)
        lines.append(f"\n## {label}（{len(items)}）\n\n")
        for r in items:
            title = r["title"] or r["id"]
            tags_str = " ".join(f"#{t}" for t in (r["tags"] or []))
            lines.append(f"- [[{subdir}/{r['id']}|{title}]] {tags_str}\n")

    wiki_dir = USER_DATA_DIR / user_id / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "index.md").write_text("".join(lines), encoding="utf-8")


async def rebuild_wiki(user_id: str) -> dict:
    rows = await database.database.fetch_all(
        "SELECT id FROM knowledge_nodes WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    for r in rows:
        await write_wiki_node(r["id"], user_id)
    await write_wiki_index(user_id)
    return {"rebuilt": len(rows)}


# Backward-compatible aliases while callers are migrated.
_wiki_subdir = wiki_subdir
_wiki_file_path = wiki_file_path
_read_wiki_body = read_wiki_body

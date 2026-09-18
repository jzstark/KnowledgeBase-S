"""Keep document type metadata consistent across source and knowledge records."""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import database
from kb.common import USER_DATA_DIR, split_frontmatter
from settings import settings


class DocumentTypeError(ValueError):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class DocumentTypeResult:
    article_ids: list[str] = field(default_factory=list)
    summary_ids: list[str] = field(default_factory=list)
    wiki_warnings: list[str] = field(default_factory=list)


def validate_explicit_doc_kind(value: str) -> str:
    allowed = set(settings.doc_kind.values)
    if not value or (allowed and value not in allowed):
        raise DocumentTypeError(
            f"无效的内容类型；可选值：{', '.join(sorted(allowed))}"
        )
    return value


def _replace_frontmatter_doc_kind(path: Path, doc_kind: str) -> None:
    mode = path.stat().st_mode
    raw = path.read_text(encoding="utf-8")
    frontmatter_text, body = split_frontmatter(raw)
    if not frontmatter_text:
        raise ValueError("文件缺少 YAML frontmatter")
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("YAML frontmatter 不是对象")
    frontmatter["doc_kind"] = doc_kind
    serialized = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    content = f"---\n{serialized}---\n{body}"

    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        temp_path.chmod(mode)
        temp_path.replace(path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def sync_wiki_doc_kind(
    article_ids: list[str], summary_ids: list[str], doc_kind: str, *, user_id: str = "default"
) -> list[str]:
    """Best-effort derived-file sync; retrying the DB operation retries these writes."""
    warnings: list[str] = []
    for directory, node_ids in (("articles", article_ids), ("summaries", summary_ids)):
        for node_id in node_ids:
            path = USER_DATA_DIR / user_id / "wiki" / directory / f"{node_id}.md"
            if not path.exists():
                if directory == "articles":
                    warnings.append(f"{node_id} 的 Wiki 文件不存在")
                continue
            try:
                _replace_frontmatter_doc_kind(path, doc_kind)
            except Exception as exc:
                warnings.append(f"{node_id} 的 Wiki 元数据同步失败：{exc}")
    return warnings


async def _related_node_ids(
    *, document_instance_id: str | None = None, source_item_id: str | None = None
) -> tuple[list[str], list[str]]:
    if document_instance_id:
        article_rows = await database.database.fetch_all(
            """
            SELECT DISTINCT an.node_id
            FROM article_nodes an
            WHERE an.document_instance_id = :di_id
               OR an.source_item_id IN (
                    SELECT id FROM source_items WHERE document_instance_id = :di_id
               )
            """,
            {"di_id": document_instance_id},
        )
    else:
        article_rows = await database.database.fetch_all(
            "SELECT node_id FROM article_nodes WHERE source_item_id = :si_id",
            {"si_id": source_item_id},
        )
    article_ids = [row["node_id"] for row in article_rows]
    if not article_ids:
        return [], []
    summary_rows = await database.database.fetch_all(
        "SELECT node_id FROM summary_nodes WHERE summary_of = ANY(:article_ids)",
        {"article_ids": article_ids},
    )
    return article_ids, [row["node_id"] for row in summary_rows]


async def _update_knowledge_node_types(
    article_ids: list[str], summary_ids: list[str], doc_kind: str
) -> None:
    node_ids = article_ids + summary_ids
    if not node_ids:
        return
    await database.database.execute(
        """
        UPDATE knowledge_nodes
        SET doc_kind = :doc_kind, updated_at = NOW()
        WHERE id = ANY(:node_ids)
        """,
        {"doc_kind": doc_kind, "node_ids": node_ids},
    )


async def set_document_instance_doc_kind(
    document_instance_id: str,
    doc_kind: str,
    *,
    user_id: str = "default",
    folder_id: str | None = None,
) -> DocumentTypeResult:
    doc_kind = validate_explicit_doc_kind(doc_kind)
    async with database.database.transaction():
        document = await database.database.fetch_one(
            """
            SELECT id, folder_id, status
            FROM document_instances
            WHERE id = :id AND user_id = :uid
            FOR UPDATE
            """,
            {"id": document_instance_id, "uid": user_id},
        )
        if not document:
            raise DocumentTypeError("文档不存在", 404)
        if folder_id is not None and document["folder_id"] != folder_id:
            raise DocumentTypeError("文档不属于当前资料夹")
        if document["status"] == "deleted":
            raise DocumentTypeError("文档已永久删除", 409)

        source_items = await database.database.fetch_all(
            """
            SELECT id, status
            FROM source_items
            WHERE document_instance_id = :id
            FOR UPDATE
            """,
            {"id": document_instance_id},
        )
        if document["status"] == "processing" or any(
            item["status"] == "processing" for item in source_items
        ):
            raise DocumentTypeError("文档正在处理，请完成后再修改类型", 409)

        article_ids, summary_ids = await _related_node_ids(
            document_instance_id=document_instance_id
        )
        await database.database.execute(
            """
            UPDATE document_instances
            SET doc_kind = :doc_kind, updated_at = NOW()
            WHERE id = :id
            """,
            {"id": document_instance_id, "doc_kind": doc_kind},
        )
        await database.database.execute(
            """
            UPDATE source_items
            SET doc_kind = :doc_kind, updated_at = NOW()
            WHERE document_instance_id = :id
            """,
            {"id": document_instance_id, "doc_kind": doc_kind},
        )
        await _update_knowledge_node_types(article_ids, summary_ids, doc_kind)

    return DocumentTypeResult(
        article_ids=article_ids,
        summary_ids=summary_ids,
        wiki_warnings=sync_wiki_doc_kind(
            article_ids, summary_ids, doc_kind, user_id=user_id
        ),
    )


async def set_source_item_doc_kind(
    source_item_id: str,
    doc_kind: str | None,
    *,
    user_id: str = "default",
) -> DocumentTypeResult:
    """Keep the legacy source-item editor consistent with the folder UI."""
    if doc_kind is not None:
        doc_kind = validate_explicit_doc_kind(doc_kind)

    item = await database.database.fetch_one(
        """
        SELECT si.document_instance_id, si.source_id, s.default_doc_kind
        FROM source_items si
        LEFT JOIN sources s ON s.id = si.source_id
        WHERE si.id = :id AND si.user_id = :uid
        """,
        {"id": source_item_id, "uid": user_id},
    )
    if not item:
        raise DocumentTypeError("source item 不存在", 404)
    if item["document_instance_id"] and doc_kind is not None:
        return await set_document_instance_doc_kind(
            item["document_instance_id"], doc_kind, user_id=user_id
        )

    effective_doc_kind = doc_kind or item["default_doc_kind"] or settings.doc_kind.default
    async with database.database.transaction():
        locked_item = await database.database.fetch_one(
            """
            SELECT id, status, document_instance_id
            FROM source_items
            WHERE id = :id AND user_id = :uid
            FOR UPDATE
            """,
            {"id": source_item_id, "uid": user_id},
        )
        if not locked_item:
            raise DocumentTypeError("source item 不存在", 404)
        if locked_item["status"] == "processing":
            raise DocumentTypeError("文档正在处理，请完成后再修改类型", 409)
        if locked_item["document_instance_id"]:
            document = await database.database.fetch_one(
                """
                SELECT status
                FROM document_instances
                WHERE id = :id AND user_id = :uid
                FOR UPDATE
                """,
                {"id": locked_item["document_instance_id"], "uid": user_id},
            )
            if document and document["status"] == "processing":
                raise DocumentTypeError("文档正在处理，请完成后再修改类型", 409)

        if locked_item["document_instance_id"]:
            article_ids, summary_ids = await _related_node_ids(
                document_instance_id=locked_item["document_instance_id"]
            )
        else:
            article_ids, summary_ids = await _related_node_ids(source_item_id=source_item_id)
        if locked_item["document_instance_id"]:
            await database.database.execute(
                """
                UPDATE source_items
                SET doc_kind = :doc_kind, updated_at = NOW()
                WHERE document_instance_id = :id
                """,
                {"id": locked_item["document_instance_id"], "doc_kind": doc_kind},
            )
            await database.database.execute(
                """
                UPDATE document_instances
                SET doc_kind = :doc_kind, updated_at = NOW()
                WHERE id = :id
                """,
                {"id": locked_item["document_instance_id"], "doc_kind": doc_kind},
            )
        else:
            await database.database.execute(
                """
                UPDATE source_items
                SET doc_kind = :doc_kind, updated_at = NOW()
                WHERE id = :id
                """,
                {"id": source_item_id, "doc_kind": doc_kind},
            )
        await _update_knowledge_node_types(article_ids, summary_ids, effective_doc_kind)

    return DocumentTypeResult(
        article_ids=article_ids,
        summary_ids=summary_ids,
        wiki_warnings=sync_wiki_doc_kind(
            article_ids, summary_ids, effective_doc_kind, user_id=user_id
        ),
    )

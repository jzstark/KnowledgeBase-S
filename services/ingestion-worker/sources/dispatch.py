"""Resolve the parser for a materialized source item.

Folders can contain mixed content while their compatibility source has a single
type. The item-level source_type is therefore authoritative during processing.
"""

from typing import Any


BOOK_SOURCE_TYPES = frozenset({"book", "epub"})
FILE_SOURCE_TYPES = frozenset({"pdf", "image", "plaintext", "word"}) | BOOK_SOURCE_TYPES


def source_for_type(source_type: str, source_id: str, raw_config: dict | None = None) -> Any:
    """Build non-feed sources from the registry shared by polling and item dispatch."""
    raw_config = raw_config or {}
    if source_type == "url":
        from sources.url import URLSource

        return URLSource(source_id=source_id, url=raw_config.get("url", ""))

    if source_type not in FILE_SOURCE_TYPES:
        raise ValueError(f"unsupported source type: {source_type}")

    from sources.book import BookSource
    from sources.image import ImageSource
    from sources.pdf import PDFSource
    from sources.plaintext import PlaintextSource
    from sources.word import WordSource

    source_classes = {
        "pdf": PDFSource,
        "image": ImageSource,
        "plaintext": PlaintextSource,
        "word": WordSource,
        "epub": BookSource,
        "book": BookSource,
    }
    return source_classes[source_type](
        source_id=source_id,
        uploads=raw_config.get("uploads", []),
    )


def source_type_for_item(source_config: dict, source_item: dict) -> str:
    return source_item.get("source_type") or source_config["type"]


def is_book_source_item(source_config: dict, source_item: dict) -> bool:
    return source_type_for_item(source_config, source_item) in BOOK_SOURCE_TYPES


def source_for_item(
    default_source: Any,
    source_config: dict,
    source_item: dict,
) -> tuple[Any, str]:
    source_type = source_type_for_item(source_config, source_item)
    if source_type == source_config["type"]:
        return default_source, source_type

    try:
        return source_for_type(source_type, source_config["id"]), source_type
    except ValueError as exc:
        raise ValueError(f"unsupported source item type: {source_type}") from exc

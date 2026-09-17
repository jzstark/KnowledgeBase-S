"""Resolve the parser for a materialized source item.

Folders can contain mixed content while their compatibility source has a single
type. The item-level source_type is therefore authoritative during processing.
"""

from typing import Any


BOOK_SOURCE_TYPES = frozenset({"book", "epub"})


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

    source_id = source_config["id"]
    if source_type == "pdf":
        from sources.pdf import PDFSource

        return PDFSource(source_id=source_id, uploads=[]), source_type
    if source_type == "image":
        from sources.image import ImageSource

        return ImageSource(source_id=source_id, uploads=[]), source_type
    if source_type == "plaintext":
        from sources.plaintext import PlaintextSource

        return PlaintextSource(source_id=source_id, uploads=[]), source_type
    if source_type == "word":
        from sources.word import WordSource

        return WordSource(source_id=source_id, uploads=[]), source_type
    if source_type in BOOK_SOURCE_TYPES:
        from sources.book import BookSource

        return BookSource(source_id=source_id, uploads=[]), source_type
    if source_type == "url":
        from sources.url import URLSource

        return URLSource(source_id=source_id, url=""), source_type

    raise ValueError(f"unsupported source item type: {source_type}")

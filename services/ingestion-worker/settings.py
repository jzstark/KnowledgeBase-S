from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path("/app/shared_config/system.yaml")


@dataclass(frozen=True)
class IngestionSettings:
    max_text_chars: int = 12000
    chunk_trigger_words: int = 5000
    chunk_target_words: int = 1500
    max_entity_page_sources: int = 5
    max_index_children_abstracts: int = 20
    rss_lookback_days: int = 14
    context_nearby_entities: int = 20
    context_top_candidates: int = 20
    context_popular_tags: int = 50


@dataclass(frozen=True)
class ModelsSettings:
    article_analysis: str = "claude-haiku-4-5-20251001"
    entity_page: str = "claude-haiku-4-5-20251001"
    entity_update: str = "claude-haiku-4-5-20251001"
    summary_gen: str = "claude-haiku-4-5-20251001"
    index_summary: str = "claude-haiku-4-5-20251001"
    hyde_abstract: str = "claude-haiku-4-5-20251001"
    briefing_topics: str = "claude-haiku-4-5-20251001"
    draft_generation: str = "claude-sonnet-4-6"
    compare: str = "claude-sonnet-4-6"
    cite: str = "claude-sonnet-4-6"
    summarize_corpus: str = "claude-sonnet-4-6"
    image_ocr: str = "claude-sonnet-4-6"
    image_cleanup: str = "claude-sonnet-4-6"
    pdf_cleanup: str = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class DocKindSettings:
    values: list[str] = field(default_factory=list)
    default: str = "other"


@dataclass(frozen=True)
class EmbeddingSettings:
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    max_chars: int = 8000


@dataclass(frozen=True)
class LlmOutputTokensSettings:
    article_analysis: int = 2048
    entity_page: int = 2048
    entity_update: int = 2048
    summary_gen: int = 1024
    index_summary: int = 512
    hyde_abstract: int = 200
    briefing_topics: int = 8192
    draft_generation: int = 4096
    compare: int = 2048
    cite: int = 2048
    summarize_corpus: int = 3000
    image_ocr: int = 4096
    image_cleanup: int = 4096
    pdf_cleanup: int = 4096


@dataclass(frozen=True)
class Settings:
    ingestion: IngestionSettings
    models: ModelsSettings
    embedding: EmbeddingSettings
    llm_output_tokens: LlmOutputTokensSettings
    doc_kind: DocKindSettings

    @classmethod
    def load(cls, path: Path = _DEFAULT_PATH) -> Settings:
        try:
            data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            warnings.warn(f"system.yaml not found at {path}; using defaults")
            data = {}

        def sub(key: str) -> dict[str, Any]:
            return data.get(key) or {}

        return cls(
            ingestion=IngestionSettings(**sub("ingestion")),
            models=ModelsSettings(**sub("models")),
            embedding=EmbeddingSettings(**sub("embedding")),
            llm_output_tokens=LlmOutputTokensSettings(**sub("llm_output_tokens")),
            doc_kind=DocKindSettings(**sub("doc_kind")),
        )


settings = Settings.load()

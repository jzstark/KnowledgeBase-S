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
    max_index_children_abstracts: int = 20
    rss_lookback_days: int = 14
    context_nearby_entities: int = 20
    context_top_candidates: int = 20
    context_popular_tags: int = 50


@dataclass(frozen=True)
class ModelsSettings:
    article_analysis: str = "claude-haiku-4-5-20251001"
    entity_update: str = "claude-haiku-4-5-20251001"
    summary_gen: str = "claude-haiku-4-5-20251001"
    index_summary: str = "claude-haiku-4-5-20251001"
    hyde_abstract: str = "claude-haiku-4-5-20251001"
    compare: str = "claude-sonnet-5-5"
    cite: str = "claude-sonnet-5-5"
    summarize_corpus: str = "claude-sonnet-5-5"
    image_ocr: str = "claude-haiku-4-5-20251001"
    image_cleanup: str = "claude-sonnet-5-5"
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
    entity_update: int = 16000
    summary_gen: int = 1024
    index_summary: int = 512
    hyde_abstract: int = 200
    compare: int = 8192
    cite: int = 8192
    summarize_corpus: int = 8192
    image_ocr: int = 4096
    image_cleanup: int = 8192
    pdf_cleanup: int = 4096


@dataclass(frozen=True)
class LlmEffortSettings:
    """Effort per model route; empty for models that do not accept effort (Haiku 4.5)."""
    article_analysis: str = ""
    entity_update: str = ""
    summary_gen: str = ""
    index_summary: str = ""
    hyde_abstract: str = ""
    compare: str = "low"
    cite: str = "low"
    summarize_corpus: str = "low"
    image_ocr: str = ""
    image_cleanup: str = "low"
    pdf_cleanup: str = ""


@dataclass(frozen=True)
class Settings:
    ingestion: IngestionSettings
    models: ModelsSettings
    embedding: EmbeddingSettings
    llm_output_tokens: LlmOutputTokensSettings
    llm_effort: LlmEffortSettings
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
            llm_effort=LlmEffortSettings(**sub("llm_effort")),
            doc_kind=DocKindSettings(**sub("doc_kind")),
        )

    def model_params(self, route: str) -> dict[str, Any]:
        """Model, output cap and effort for one configured model route."""
        params: dict[str, Any] = {
            "model": getattr(self.models, route),
            "max_tokens": getattr(self.llm_output_tokens, route),
        }
        effort = getattr(self.llm_effort, route)
        if effort:
            params["output_config"] = {"effort": effort}
        return params


settings = Settings.load()

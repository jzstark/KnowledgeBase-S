from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path("/app/shared_config/system.yaml")


@dataclass(frozen=True)
class DocKindSettings:
    values: list[str] = field(default_factory=list)
    default: str = "other"


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
class EmbeddingSettings:
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    max_chars: int = 8000


@dataclass(frozen=True)
class EntitySettings:
    promotion_max_salience: float = 0.9
    promotion_salience: float = 0.7
    promotion_salience_mentions: int = 2
    promotion_min_mentions: int = 3


@dataclass(frozen=True)
class RetrievalSettings:
    use_hyde: bool = True
    similar_to_threshold: float = 0.75
    similar_to_limit: int = 20
    co_occurs_min_articles: int = 3
    summary_top_k: int = 5
    entity_top_k: int = 10
    article_direct_top_k: int = 8
    article_top_k: int = 8
    entity_in_context: int = 5
    article_inline_threshold: int = 2000
    context_max_tokens: int = 100000
    draft_knowledge_chars: int = 6000
    damping_entity_to_summary: float = 0.7
    damping_hop: float = 0.3
    expansion_anchor_k: int = 5
    expansion_min_score: float = 0.3
    index_expand_threshold: float = 0.4
    index_expand_limit: int = 3
    fallback_score_discount: float = 0.5


@dataclass(frozen=True)
class MaintenanceSettings:
    entity_update_batch: int = 10
    rebuild_max_wait_seconds: int = 3600
    rebuild_poll_interval_seconds: int = 20


@dataclass(frozen=True)
class BriefingSettings:
    topics_count: int = 5
    hours_back: int = 24
    batch_size: int = 12
    summary_chars: int = 150


@dataclass(frozen=True)
class DraftsSettings:
    min_remaining_chars: int = 100


@dataclass(frozen=True)
class EntityInsightsSettings:
    refresh_facts_limit: int = 12


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
class KbPublicSettings:
    search_top_k: int = 10
    search_top_k_max: int = 50
    fetch_max_batch: int = 20
    fetch_body_chars: int = 100000
    related_default_limit: int = 20
    related_limit_max: int = 100
    timeline_default_limit: int = 50
    timeline_limit_max: int = 200
    timeline_min_score: float = 0.3
    compare_body_chars: int = 4000
    cite_max_results: int = 5
    cite_candidate_count: int = 20
    cite_body_chars: int = 3000
    summarize_max_sources: int = 10
    summarize_body_chars: int = 2500
    summarize_summary_min_score: float = 0.3


@dataclass(frozen=True)
class Settings:
    doc_kind: DocKindSettings
    ingestion: IngestionSettings
    models: ModelsSettings
    embedding: EmbeddingSettings
    entity: EntitySettings
    retrieval: RetrievalSettings
    maintenance: MaintenanceSettings
    briefing: BriefingSettings
    drafts: DraftsSettings
    entity_insights: EntityInsightsSettings
    llm_output_tokens: LlmOutputTokensSettings
    kb_public: KbPublicSettings

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
            doc_kind=DocKindSettings(**sub("doc_kind")),
            ingestion=IngestionSettings(**sub("ingestion")),
            models=ModelsSettings(**sub("models")),
            embedding=EmbeddingSettings(**sub("embedding")),
            entity=EntitySettings(**sub("entity")),
            retrieval=RetrievalSettings(**sub("retrieval")),
            maintenance=MaintenanceSettings(**sub("maintenance")),
            briefing=BriefingSettings(**sub("briefing")),
            drafts=DraftsSettings(**sub("drafts")),
            entity_insights=EntityInsightsSettings(**sub("entity_insights")),
            llm_output_tokens=LlmOutputTokensSettings(**sub("llm_output_tokens")),
            kb_public=KbPublicSettings(**sub("kb_public")),
        )


settings = Settings.load()

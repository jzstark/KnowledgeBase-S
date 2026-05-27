"""
Loads /app/shared_config/system.yaml (bind-mounted from repo config/).
Provides dot-path access: get("retrieval.entity_top_k", 10)
"""
import yaml
from pathlib import Path

_PATH = Path("/app/shared_config/system.yaml")

REQUIRED_KEYS = (
    "doc_kind.values",
    "doc_kind.default",
    "ingestion.max_text_chars",
    "ingestion.max_entity_page_sources",
    "ingestion.max_index_children_abstracts",
    "ingestion.rss_lookback_days",
    "ingestion.context_nearby_entities",
    "ingestion.context_top_candidates",
    "ingestion.context_popular_tags",
    "models.article_analysis",
    "models.entity_page",
    "models.summary_gen",
    "models.index_summary",
    "models.hyde_abstract",
    "models.briefing_topics",
    "models.draft_generation",
    "models.compare",
    "models.cite",
    "models.summarize_corpus",
    "embedding.model",
    "embedding.dimensions",
    "embedding.max_chars",
    "entity.promotion_max_salience",
    "entity.promotion_salience",
    "entity.promotion_salience_mentions",
    "entity.promotion_min_mentions",
    "retrieval.use_hyde",
    "retrieval.similar_to_threshold",
    "retrieval.similar_to_limit",
    "retrieval.summary_top_k",
    "retrieval.entity_top_k",
    "retrieval.article_direct_top_k",
    "retrieval.article_top_k",
    "retrieval.entity_in_context",
    "retrieval.draft_knowledge_chars",
    "retrieval.damping_entity_to_summary",
    "retrieval.damping_hop",
    "retrieval.expansion_anchor_k",
    "retrieval.expansion_min_score",
    "retrieval.index_expand_threshold",
    "retrieval.index_expand_limit",
    "retrieval.fallback_score_discount",
    "maintenance.rebuild_max_wait_seconds",
    "maintenance.rebuild_poll_interval_seconds",
    "briefing.hours_back",
    "briefing.batch_size",
    "briefing.summary_chars",
    "llm_output_tokens.article_analysis",
    "llm_output_tokens.entity_page",
    "llm_output_tokens.summary_gen",
    "llm_output_tokens.index_summary",
    "llm_output_tokens.hyde_abstract",
    "llm_output_tokens.briefing_topics",
    "llm_output_tokens.draft_generation",
    "llm_output_tokens.compare",
    "llm_output_tokens.cite",
    "llm_output_tokens.summarize_corpus",
    "kb_public.search_top_k",
    "kb_public.search_top_k_max",
    "kb_public.fetch_max_batch",
    "kb_public.fetch_body_chars",
    "kb_public.related_default_limit",
    "kb_public.related_limit_max",
    "kb_public.timeline_default_limit",
    "kb_public.timeline_limit_max",
    "kb_public.timeline_min_score",
    "kb_public.compare_body_chars",
    "kb_public.cite_max_results",
    "kb_public.cite_candidate_count",
    "kb_public.cite_body_chars",
    "kb_public.summarize_max_sources",
    "kb_public.summarize_body_chars",
    "kb_public.summarize_summary_min_score",
)

try:
    _cfg: dict = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
except FileNotFoundError:
    import warnings
    warnings.warn(f"system.yaml not found at {_PATH}; all config values will use defaults")
    _cfg = {}


def get(path: str, default=None):
    """Return config value at dot-separated path, or default if not found."""
    keys = path.split(".")
    v = _cfg
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k)
        if v is None:
            return default
    return v


def validate_required_keys() -> None:
    """Fail startup if required system.yaml keys are missing."""
    missing = [path for path in REQUIRED_KEYS if get(path, None) is None]
    if missing:
        raise RuntimeError(f"Missing required config keys in {_PATH}: {', '.join(missing)}")

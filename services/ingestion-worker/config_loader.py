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
    "ingestion.rss_lookback_days",
    "ingestion.context_nearby_entities",
    "ingestion.context_top_candidates",
    "ingestion.context_popular_tags",
    "models.article_analysis",
    "models.entity_page",
    "models.image_ocr",
    "models.image_cleanup",
    "models.pdf_cleanup",
    "embedding.model",
    "embedding.dimensions",
    "embedding.max_chars",
    "llm_output_tokens.article_analysis",
    "llm_output_tokens.entity_page",
    "llm_output_tokens.image_ocr",
    "llm_output_tokens.image_cleanup",
    "llm_output_tokens.pdf_cleanup",
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

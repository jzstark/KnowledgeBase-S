"""
Loads /app/shared_config/system.yaml (bind-mounted from repo config/).
Provides dot-path access: get("models.feedback_analysis", "...").
"""
from pathlib import Path
from typing import Any

import yaml

_PATH = Path("/app/shared_config/system.yaml")

REQUIRED_KEYS = (
    "models.feedback_analysis",
    "llm_output_tokens.feedback_analysis",
)

try:
    _cfg: dict[str, Any] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
except FileNotFoundError:
    import warnings
    warnings.warn(f"system.yaml not found at {_PATH}; all config values will use defaults")
    _cfg = {}


def get(path: str, default: Any = None) -> Any:
    keys = path.split(".")
    value: Any = _cfg
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
        if value is None:
            return default
    return value


def validate_required_keys() -> None:
    missing = [path for path in REQUIRED_KEYS if get(path, None) is None]
    if missing:
        raise RuntimeError(f"Missing required config keys in {_PATH}: {', '.join(missing)}")

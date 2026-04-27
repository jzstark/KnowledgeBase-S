"""
Loads /app/shared_config/system.yaml (bind-mounted from repo config/).
Provides dot-path access: get("retrieval.entity_top_k", 10)
"""
import yaml
from pathlib import Path

_PATH = Path("/app/shared_config/system.yaml")

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

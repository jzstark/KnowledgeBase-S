"""
Prompt loader — reads /app/shared_config/prompts.md (bind-mounted from repo config/).
Sections are delimited by "## key" headers; placeholders use <<<key>>> syntax.
"""
import re
import os
from pathlib import Path

_DEFAULT_PATH = Path("/app/shared_config/prompts.md")
_LOCAL_PATH = Path(__file__).resolve().parents[2] / "config" / "prompts.md"
_PATH = Path(os.environ.get("PROMPTS_PATH", str(_DEFAULT_PATH)))

REQUIRED_PROMPTS = (
    "entity_page",
    "summary_gen",
    "briefing_topics",
    "hyde_abstract",
    "index_summary",
    "compare_nodes",
    "cite_match",
    "summarize_corpus",
)


def _load() -> dict[str, str]:
    path = _PATH if _PATH.exists() else _LOCAL_PATH
    text = path.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    for m in re.finditer(r"^## (\S+)\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    return result


_PROMPTS = _load()


def get(name: str) -> str:
    """Return the raw prompt text for the given section name."""
    return _PROMPTS[name]


def fill(name: str, **kwargs: str) -> str:
    """Return the prompt with <<<key>>> placeholders replaced by kwargs values."""
    text = _PROMPTS[name]
    for k, v in kwargs.items():
        text = text.replace(f"<<<{k}>>>", v)
    return text


def validate_required_prompts() -> None:
    missing = [name for name in REQUIRED_PROMPTS if name not in _PROMPTS]
    if missing:
        raise RuntimeError(f"Missing required prompts in {_PATH}: {', '.join(missing)}")

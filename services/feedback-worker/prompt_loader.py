"""
Prompt loader — reads /app/shared_config/prompts.md (bind-mounted from repo config/).
Sections are delimited by "## key" headers; placeholders use <<<key>>> syntax.
"""
import re
from pathlib import Path

_PATH = Path("/app/shared_config/prompts.md")


def _load() -> dict[str, str]:
    text = _PATH.read_text(encoding="utf-8")
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

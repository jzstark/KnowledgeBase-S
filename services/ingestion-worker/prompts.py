from __future__ import annotations

import os
import re
from pathlib import Path

_DEFAULT_PATH = Path("/app/shared_config/prompts.md")
try:
    _LOCAL_PATH = Path(__file__).resolve().parents[2] / "config" / "prompts.md"
except IndexError:
    _LOCAL_PATH = _DEFAULT_PATH

_REQUIRED = frozenset({
    "article_analysis",
    "entity_page",
    "image_ocr",
    "image_cleanup",
    "pdf_cleanup",
})


def _parse(path: Path) -> dict[str, str]:
    resolved = path if path.exists() else _LOCAL_PATH
    text = resolved.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    for m in re.finditer(r"^## (\S+)\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL):
        result[m.group(1)] = m.group(2).strip()
    return result


def _fill(template: str, **kwargs: str) -> str:
    for k, v in kwargs.items():
        template = template.replace(f"<<<{k}>>>", v)
    return template


class Prompts:
    def __init__(self, raw: dict[str, str]) -> None:
        self._raw = raw

    @classmethod
    def load(cls, path: Path | None = None) -> Prompts:
        resolved = path or Path(os.environ.get("PROMPTS_PATH", str(_DEFAULT_PATH)))
        raw = _parse(resolved)
        missing = _REQUIRED - raw.keys()
        if missing:
            raise RuntimeError(f"Missing required prompts: {', '.join(sorted(missing))}")
        return cls(raw)

    def article_analysis(self, *, text: str, existing_entities: str, candidate_entities: str, existing_tags: str) -> str:
        return _fill(self._raw["article_analysis"], text=text, existing_entities=existing_entities, candidate_entities=candidate_entities, existing_tags=existing_tags)

    def entity_page(self, *, entity_name: str, aliases: str, source_abstracts: str) -> str:
        return _fill(self._raw["entity_page"], entity_name=entity_name, aliases=aliases, source_abstracts=source_abstracts)

    def image_ocr(self) -> str:
        return self._raw["image_ocr"]

    def image_cleanup(self) -> str:
        return self._raw["image_cleanup"]

    def pdf_cleanup(self) -> str:
        return self._raw["pdf_cleanup"]


prompts = Prompts.load()

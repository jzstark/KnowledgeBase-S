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
    "entity_page",
    "summary_gen",
    "briefing_topics",
    "hyde_abstract",
    "index_summary",
    "compare_nodes",
    "cite_match",
    "summarize_corpus",
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

    def hyde_abstract(self, *, topic: str) -> str:
        return _fill(self._raw["hyde_abstract"], topic=topic)

    def summary_gen(self, *, title: str, abstract: str, body: str, perspective_instruction: str) -> str:
        return _fill(self._raw["summary_gen"], title=title, abstract=abstract, body=body, perspective_instruction=perspective_instruction)

    def entity_page(self, *, entity_name: str, aliases: str, source_abstracts: str) -> str:
        return _fill(self._raw["entity_page"], entity_name=entity_name, aliases=aliases, source_abstracts=source_abstracts)

    def index_summary(self, *, index_title: str, child_abstracts: str) -> str:
        return _fill(self._raw["index_summary"], index_title=index_title, child_abstracts=child_abstracts)

    def briefing_topics(self, *, topics_setting: str, summaries: str) -> str:
        return _fill(self._raw["briefing_topics"], topics_setting=topics_setting, summaries=summaries)

    def compare_nodes(self, *, documents: str, dimensions: str, focus: str) -> str:
        return _fill(self._raw["compare_nodes"], documents=documents, dimensions=dimensions, focus=focus)

    def cite_match(self, *, claim: str, context: str, candidates: str) -> str:
        return _fill(self._raw["cite_match"], claim=claim, context=context, candidates=candidates)

    def summarize_corpus(self, *, documents: str, focus: str, output_format: str) -> str:
        return _fill(self._raw["summarize_corpus"], documents=documents, focus=focus, output_format=output_format)


prompts = Prompts.load()

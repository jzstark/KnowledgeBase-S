import os
import pathlib

USER_ID = "default"
USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
LEGACY_LLM_EDGE_TYPES = {"extends", "background_of", "supports", "contradicts", "part_of"}


def is_visible_edge(relation_type: str | None) -> bool:
    return relation_type not in LEGACY_LLM_EDGE_TYPES


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in values) + "]"


def split_frontmatter(raw: str) -> tuple[str, str]:
    """Split a wiki markdown file into (frontmatter_text, body_text).

    The frontmatter is the block between the leading ``---`` line and the next
    line that is exactly ``---``. Matching on the whole *line* (not the substring
    ``---``) means a frontmatter value or body that itself contains ``---`` can't
    corrupt the parse. Returns ("", raw) when there is no frontmatter block.
    """
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", raw
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    return "", raw


# Backward-compatible aliases while router code is being untangled.
_is_visible_edge = is_visible_edge
_vector_literal = vector_literal

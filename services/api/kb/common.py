import os
import pathlib

USER_ID = "default"
USER_DATA_DIR = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
LEGACY_LLM_EDGE_TYPES = {"extends", "background_of", "supports", "contradicts", "part_of"}


def is_visible_edge(relation_type: str | None) -> bool:
    return relation_type not in LEGACY_LLM_EDGE_TYPES


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in values) + "]"


# Backward-compatible aliases while router code is being untangled.
_is_visible_edge = is_visible_edge
_vector_literal = vector_literal

import os

import anthropic
from openai import AsyncOpenAI

from settings import settings
from prompts import prompts
from kb.common import message_text, vector_literal

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
claude_client = anthropic.AsyncAnthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))


async def embed_text(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model=settings.embedding.model,
        input=text[: settings.embedding.max_chars],
        dimensions=settings.embedding.dimensions,
    )
    return resp.data[0].embedding


async def embed_query(text: str) -> list[float]:
    """Embed query text, optionally via HyDE."""
    if not settings.retrieval.use_hyde:
        return await embed_text(text)
    try:
        hypo = await claude_client.messages.create(
            model=settings.models.hyde_abstract,
            max_tokens=settings.llm_output_tokens.hyde_abstract,
            messages=[{"role": "user", "content": prompts.hyde_abstract(topic=text)}],
        )
        hypo_text = message_text(hypo)
        if hypo_text:
            return await embed_text(hypo_text)
    except Exception:
        pass
    return await embed_text(text)


# Backward-compatible aliases while callers are migrated.
_embed_text = embed_text
_embed_query = embed_query
_hyde_embed_query = embed_query
_vector_literal = vector_literal

import os

import anthropic
from openai import AsyncOpenAI

import config_loader
import prompt_loader
from kb.common import vector_literal

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
claude_client = anthropic.AsyncAnthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))


async def embed_text(text: str) -> list[float]:
    resp = await openai_client.embeddings.create(
        model=config_loader.get("embedding.model", "text-embedding-3-small"),
        input=text[: config_loader.get("embedding.max_chars", 8000)],
        dimensions=config_loader.get("embedding.dimensions", 1536),
    )
    return resp.data[0].embedding


async def embed_query(text: str) -> list[float]:
    """Embed query text, optionally via HyDE."""
    if not config_loader.get("retrieval.use_hyde", True):
        return await embed_text(text)
    try:
        hypo = await claude_client.messages.create(
            model=config_loader.get("models.hyde_abstract", "claude-haiku-4-5-20251001"),
            max_tokens=config_loader.get("llm_output_tokens.hyde_abstract", 200),
            messages=[{"role": "user", "content": prompt_loader.fill("hyde_abstract", topic=text)}],
        )
        hypo_text = getattr(hypo.content[0], "text", "").strip()
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

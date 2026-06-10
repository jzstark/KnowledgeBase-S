import json
import os

import databases

DATABASE_URL = os.environ["DATABASE_URL"]

database = databases.Database(DATABASE_URL)


def jsonb(value: dict) -> str:
    """将 dict 序列化为 JSON 字符串，供 asyncpg JSONB 参数使用。"""
    return json.dumps(value, ensure_ascii=False)


async def init():
    """Open the connection pool.

    The schema is owned by Alembic and applied out-of-band by the api container
    entrypoint (`alembic upgrade head`, gated on RUN_MIGRATIONS=1); see
    services/api/alembic/. Workers that call init() only connect — they never
    run DDL.
    """
    await database.connect()


async def validate_embedding_dimension(expected: int) -> None:
    """Fail fast if the configured embedding dimension diverges from the schema.

    The vector columns are a fixed size set by the Alembic baseline. If
    settings.embedding.dimensions no longer matches that size, every insert of a
    freshly generated embedding would error (or silently mismatch), so surface
    the misconfiguration at startup rather than at write time. For pgvector the
    column's typmod is the dimension directly.
    """
    row = await database.fetch_one(
        """
        SELECT a.atttypmod AS dim
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'knowledge_nodes' AND a.attname = 'embedding'
        """
    )
    if row is None:
        return
    db_dim = row["dim"]
    if db_dim and db_dim > 0 and db_dim != expected:
        raise RuntimeError(
            f"Embedding dimension mismatch: settings.embedding.dimensions={expected} "
            f"but knowledge_nodes.embedding is vector({db_dim}). Update the config to "
            "match, or add a migration to ALTER the vector columns to the new size."
        )

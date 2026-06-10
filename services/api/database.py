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

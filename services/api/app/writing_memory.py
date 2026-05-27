from fastapi import APIRouter, Depends
from pydantic import BaseModel

import database
from auth import require_auth
from kb.common import USER_ID

router = APIRouter(prefix="/api/kb/memory", tags=["app:writing_memory"])


class MemoryFeedback(BaseModel):
    template_name: str
    rule: str
    rule_type: str


@router.post("/feedback")
async def add_memory(body: MemoryFeedback):
    """写入或更新偏好规则。同一 (template_name, rule) 已存在则 confidence +0.15。"""
    existing = await database.database.fetch_one(
        """
        SELECT id, confidence, count FROM writing_memory
        WHERE user_id = :user_id AND template_name = :template_name AND rule = :rule
        """,
        {"user_id": USER_ID, "template_name": body.template_name, "rule": body.rule},
    )

    if existing:
        new_confidence = min(1.0, float(existing["confidence"]) + 0.15)
        await database.database.execute(
            """
            UPDATE writing_memory
            SET confidence = :confidence, count = count + 1, updated_at = NOW()
            WHERE id = :id
            """,
            {"confidence": new_confidence, "id": existing["id"]},
        )
        return {"updated": True, "confidence": new_confidence}

    await database.database.execute(
        """
        INSERT INTO writing_memory (user_id, template_name, rule, rule_type)
        VALUES (:user_id, :template_name, :rule, :rule_type)
        """,
        {
            "user_id": USER_ID,
            "template_name": body.template_name,
            "rule": body.rule,
            "rule_type": body.rule_type,
        },
    )
    return {"updated": False, "confidence": 0.5}


@router.get("")
async def get_memory(
    template_name: str | None = None,
    min_confidence: float = 0.0,
):
    """读取偏好规则，按置信度降序。无需认证（供 worker 调用）。"""
    if template_name:
        rows = await database.database.fetch_all(
            """
            SELECT * FROM writing_memory
            WHERE user_id = :user_id AND template_name = :template_name
              AND confidence >= :min_confidence
            ORDER BY confidence DESC
            """,
            {"user_id": USER_ID, "template_name": template_name, "min_confidence": min_confidence},
        )
    else:
        rows = await database.database.fetch_all(
            """
            SELECT * FROM writing_memory
            WHERE user_id = :user_id AND confidence >= :min_confidence
            ORDER BY confidence DESC
            """,
            {"user_id": USER_ID, "min_confidence": min_confidence},
        )
    return [dict(r) for r in rows]


@router.delete("/{memory_id}")
async def delete_memory(memory_id: int, _: dict = Depends(require_auth)):
    await database.database.execute(
        "DELETE FROM writing_memory WHERE id = :id AND user_id = :user_id",
        {"id": memory_id, "user_id": USER_ID},
    )
    return {"ok": True}

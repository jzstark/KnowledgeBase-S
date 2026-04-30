"""
AI 对话路由。

GET    /api/chat/sessions                  — 列出所有会话（按时间倒序）
POST   /api/chat/sessions                  — 创建新会话
DELETE /api/chat/sessions/{id}             — 删除会话（消息级联删除）
GET    /api/chat/sessions/{id}/messages    — 获取会话消息列表
POST   /api/chat/sessions/{id}/messages    — 发送消息，SSE 流式返回 Claude 回复
"""

import json
import os
import secrets

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import database
from auth import require_auth

router = APIRouter(prefix="/api/chat", tags=["chat"])

USER_ID = "default"
CONTEXT_WINDOW = 20  # 最近消息条数

claude = anthropic.AsyncAnthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))

SYSTEM_PROMPT = "你是一个知识库助手，在个人知识管理系统中协助用户。"


class CreateSessionRequest(BaseModel):
    title: str | None = None


class SendMessageRequest(BaseModel):
    content: str


# ── 会话管理 ──────────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(_: dict = Depends(require_auth)):
    rows = await database.database.fetch_all(
        "SELECT id, title, created_at, updated_at FROM chat_sessions "
        "WHERE user_id = :uid ORDER BY updated_at DESC LIMIT 50",
        {"uid": USER_ID},
    )
    return [dict(r) for r in rows]


@router.post("/sessions")
async def create_session(body: CreateSessionRequest, _: dict = Depends(require_auth)):
    sid = f"cs_{secrets.token_hex(8)}"
    await database.database.execute(
        "INSERT INTO chat_sessions (id, user_id, title) VALUES (:id, :uid, :title)",
        {"id": sid, "uid": USER_ID, "title": body.title},
    )
    return {"id": sid, "title": body.title}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT id FROM chat_sessions WHERE id = :id AND user_id = :uid",
        {"id": session_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "会话不存在")
    await database.database.execute(
        "DELETE FROM chat_sessions WHERE id = :id", {"id": session_id}
    )
    return {"ok": True}


# ── 消息 ──────────────────────────────────────────────────────────────────────

@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, _: dict = Depends(require_auth)):
    row = await database.database.fetch_one(
        "SELECT id FROM chat_sessions WHERE id = :id AND user_id = :uid",
        {"id": session_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "会话不存在")
    rows = await database.database.fetch_all(
        "SELECT id, role, content, created_at FROM chat_messages "
        "WHERE session_id = :sid ORDER BY created_at",
        {"sid": session_id},
    )
    return [dict(r) for r in rows]


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    _: dict = Depends(require_auth),
):
    if not body.content.strip():
        raise HTTPException(400, "消息不能为空")

    row = await database.database.fetch_one(
        "SELECT id FROM chat_sessions WHERE id = :id AND user_id = :uid",
        {"id": session_id, "uid": USER_ID},
    )
    if not row:
        raise HTTPException(404, "会话不存在")

    # 保存用户消息
    await database.database.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (:sid, :role, :content)",
        {"sid": session_id, "role": "user", "content": body.content.strip()},
    )

    # 更新会话时间戳（title 为空时用首条消息前20字）
    await database.database.execute(
        """
        UPDATE chat_sessions
        SET updated_at = NOW(),
            title = COALESCE(NULLIF(title, ''), LEFT(:first, 20))
        WHERE id = :id
        """,
        {"id": session_id, "first": body.content.strip()},
    )

    # 读取最近 CONTEXT_WINDOW 条消息作为上下文
    history = await database.database.fetch_all(
        "SELECT role, content FROM chat_messages "
        "WHERE session_id = :sid ORDER BY created_at DESC LIMIT :n",
        {"sid": session_id, "n": CONTEXT_WINDOW},
    )
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(history)]

    async def stream_response():
        full_text = ""
        try:
            async with claude.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    full_text += text
                    yield f"data: {json.dumps({'delta': text}, ensure_ascii=False)}\n\n"
        finally:
            if full_text:
                await database.database.execute(
                    "INSERT INTO chat_messages (session_id, role, content) "
                    "VALUES (:sid, :role, :content)",
                    {"sid": session_id, "role": "assistant", "content": full_text},
                )
                await database.database.execute(
                    "UPDATE chat_sessions SET updated_at = NOW() WHERE id = :id",
                    {"id": session_id},
                )
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")

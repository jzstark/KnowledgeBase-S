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
import kb_tools
from auth import require_auth

router = APIRouter(prefix="/api/chat", tags=["chat"])

USER_ID = "default"
CONTEXT_WINDOW = 20  # 最近消息条数

claude = anthropic.AsyncAnthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))

SYSTEM_PROMPT = """你是一个知识库助手，在个人知识管理系统中协助用户。

你可以使用只读知识库工具搜索、打开节点、查看邻居和来源。回答涉及知识库内容时优先使用工具，并在回答中引用节点标题或节点 id。
当前阶段禁止创建、修改或删除 summary、index、tags、entity 或任何知识库内容。"""


class CreateSessionRequest(BaseModel):
    title: str | None = None


class SendMessageRequest(BaseModel):
    content: str


def _content_block_to_dict(block) -> dict:
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    if isinstance(block, dict):
        return block
    data = {"type": getattr(block, "type", "")}
    if hasattr(block, "text"):
        data["text"] = block.text
    if hasattr(block, "id"):
        data["id"] = block.id
    if hasattr(block, "name"):
        data["name"] = block.name
    if hasattr(block, "input"):
        data["input"] = block.input
    return data


def _merge_references(existing: list[dict], incoming: list[dict]) -> list[dict]:
    seen = {r.get("id") for r in existing}
    merged = [*existing]
    for ref in incoming:
        if not ref.get("id") or ref.get("id") in seen:
            continue
        merged.append(ref)
        seen.add(ref.get("id"))
    return merged[:12]


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
        references: list[dict] = []
        try:
            tool_messages = list(messages)
            for _ in range(4):
                response = await claude.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    messages=tool_messages,
                    tools=kb_tools.READ_ONLY_TOOLS,
                )
                content_blocks = [_content_block_to_dict(block) for block in response.content]
                tool_uses = [block for block in content_blocks if block.get("type") == "tool_use"]
                if not tool_uses:
                    text = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
                    full_text += text
                    if text:
                        yield f"data: {json.dumps({'delta': text, 'references': references}, ensure_ascii=False)}\n\n"
                    break

                preface = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
                if preface:
                    full_text += preface
                    yield f"data: {json.dumps({'delta': preface}, ensure_ascii=False)}\n\n"

                tool_messages.append({"role": "assistant", "content": content_blocks})
                tool_results = []
                for tool_use in tool_uses:
                    tool_name = tool_use.get("name") or ""
                    tool_input = tool_use.get("input") or {}
                    result = await kb_tools.run_tool(tool_name, tool_input, USER_ID)
                    references = _merge_references(references, result.get("references") or [])
                    yield f"data: {json.dumps({'tool_result': {'name': tool_name, 'input': tool_input, 'result': result}, 'references': references}, ensure_ascii=False)}\n\n"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.get("id"),
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                tool_messages.append({"role": "user", "content": tool_results})
            else:
                fallback = "\n\n（工具调用次数已达上限，请缩小问题范围后重试。）"
                full_text += fallback
                yield f"data: {json.dumps({'delta': fallback, 'references': references}, ensure_ascii=False)}\n\n"
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

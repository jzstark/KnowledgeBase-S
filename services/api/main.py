from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database
from auth import create_token, require_auth, verify_password
from settings import settings
from app import briefing, drafts, settings as user_settings_module
from kb import internal as kb_internal
from kb import public as kb_public
from routers import files, sources

AUTH_COOKIE_DOMAIN = os.environ.get("AUTH_COOKIE_DOMAIN") or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init()
    yield
    await database.database.disconnect()


app = FastAPI(title="KnowledgeBase API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sources.router)
app.include_router(kb_internal.router)
app.include_router(files.router)
app.include_router(briefing.router)
app.include_router(user_settings_module.router)
app.include_router(drafts.router)

# KB Public — MCP 稳定接口子应用。挂在 /api/kb/v1/，
# 独立 OpenAPI 文档位于 /api/kb/v1/docs，由 ~/Code/kb-chat/ 的 MCP adapter 调用。
kb_public_app = FastAPI(
    title="KnowledgeBase Public API",
    description="只读 MCP 工具端点。接口稳定，变更需前向兼容。",
)
kb_public_app.include_router(kb_public.router)
app.mount("/api/kb/v1", kb_public_app)


class LoginRequest(BaseModel):
    password: str


@app.post("/api/auth/login")
async def login(body: LoginRequest, response: Response):
    if not verify_password(body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")
    token = create_token()
    response.set_cookie(
        key="token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=7 * 24 * 3600,
        domain=AUTH_COOKIE_DOMAIN,
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("token", domain=AUTH_COOKIE_DOMAIN)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(_: dict = Depends(require_auth)):
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/config/doc_kind")
async def get_doc_kind_config():
    """UI 下拉枚举源——前端不允许自由文本，所有 doc_kind 输入处都拉取此端点。"""
    return {
        "values": settings.doc_kind.values,
        "default": settings.doc_kind.default,
    }

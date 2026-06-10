from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database
from auth import (
    clear_login_attempts,
    create_token,
    login_rate_limited,
    record_failed_login,
    require_auth,
    verify_password,
)
from settings import settings
from app import settings as user_settings_module
from kb import entity as kb_entity
from kb import index_ops as kb_index_ops
from kb import ingest as kb_ingest
from kb import internal as kb_internal
from kb import public as kb_public
from kb import summary as kb_summary
from routers import files, folders as folders_module, sources

AUTH_COOKIE_DOMAIN = os.environ.get("AUTH_COOKIE_DOMAIN") or None


def _cors_allow_origins() -> list[str]:
    """Explicit CORS allowlist. The browser reaches the API same-origin via the
    Next.js rewrite, so this is normally empty; set CORS_ALLOW_ORIGINS (comma-
    separated) only for genuine cross-origin callers."""
    raw = os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    nextauth_url = os.environ.get("NEXTAUTH_URL", "").strip()
    return [nextauth_url] if nextauth_url else []


ALLOWED_ORIGINS = _cors_allow_origins()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init()
    await database.validate_embedding_dimension(settings.embedding.dimensions)
    yield
    await database.database.disconnect()


app = FastAPI(title="KnowledgeBase API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sources.router)
app.include_router(folders_module.router)
app.include_router(folders_module.di_router)
app.include_router(folders_module.connector_router)
app.include_router(kb_ingest.router)
app.include_router(kb_summary.router)
app.include_router(kb_index_ops.router)
app.include_router(kb_entity.router)
app.include_router(kb_internal.router)
app.include_router(files.router)
app.include_router(user_settings_module.router)

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


def _login_ip(request: Request) -> str:
    """Client IP behind nginx (X-Real-IP / X-Forwarded-For), for rate limiting."""
    xff = request.headers.get("x-forwarded-for", "")
    return (
        request.headers.get("x-real-ip")
        or (xff.split(",")[0].strip() if xff else "")
        or (request.client.host if request.client else "unknown")
    )


@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request, response: Response):
    ip = _login_ip(request)
    if login_rate_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试次数过多，请稍后再试",
        )
    if not verify_password(body.password):
        record_failed_login(ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="密码错误")
    clear_login_attempts(ip)
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

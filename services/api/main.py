from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database
from auth import create_token, require_auth, verify_password
from routers import briefing, drafts, files, kb, settings, sources


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
app.include_router(kb.router)
app.include_router(files.router)
app.include_router(briefing.router)
app.include_router(settings.router)
app.include_router(drafts.router)


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
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("token")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(_: dict = Depends(require_auth)):
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"status": "ok"}

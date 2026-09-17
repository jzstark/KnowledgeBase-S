import hmac
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.middleware import AuthMiddleware
from fastmcp.utilities.authorization import AuthContext
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.responses import JSONResponse

from .tools import register_tools


GOOGLE_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in OAuth mode")
    return value


def _allowed_emails() -> set[str]:
    return {
        email.strip().lower()
        for email in os.environ.get("MCP_ALLOWED_EMAILS", "").split(",")
        if email.strip()
    }


def _verified(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


async def require_allowed_email(ctx: AuthContext) -> bool:
    if ctx.token is None or not _allowed_emails():
        return False
    claims = ctx.token.claims or {}
    email = claims.get("email")
    if not isinstance(email, str) or not _verified(claims.get("email_verified")):
        return False
    return email.strip().lower() in _allowed_emails()


def build_static_mcp() -> FastMCP:
    mcp = FastMCP("knowledgebase")
    register_tools(mcp)
    return mcp


def _oauth_storage():
    storage_dir = Path(os.environ.get("MCP_OAUTH_STORAGE_DIR", "/data/oauth"))
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=storage_dir):
            pass
    except OSError as exc:
        raise RuntimeError(f"OAuth storage directory is not writable: {storage_dir}") from exc

    encryption_key = _required_env("MCP_OAUTH_STORAGE_ENCRYPTION_KEY")
    try:
        fernet = Fernet(encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("MCP_OAUTH_STORAGE_ENCRYPTION_KEY must be a valid Fernet key") from exc

    store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
    )
    return FernetEncryptionWrapper(store, fernet=fernet)


def build_oauth_mcp() -> FastMCP:
    jwt_signing_key = _required_env("MCP_OAUTH_JWT_SIGNING_KEY")
    if len(jwt_signing_key) < 32:
        raise RuntimeError("MCP_OAUTH_JWT_SIGNING_KEY must be at least 32 characters")

    provider = GoogleProvider(
        client_id=_required_env("GOOGLE_CLIENT_ID"),
        client_secret=_required_env("GOOGLE_CLIENT_SECRET"),
        base_url=_required_env("MCP_OAUTH_BASE_URL"),
        redirect_path="/auth/callback",
        required_scopes=GOOGLE_SCOPES,
        valid_scopes=GOOGLE_SCOPES,
        client_storage=_oauth_storage(),
        jwt_signing_key=jwt_signing_key,
        require_authorization_consent=True,
    )
    mcp = FastMCP(
        "knowledgebase",
        auth=provider,
        middleware=[AuthMiddleware(auth=require_allowed_email)],
    )
    register_tools(mcp)
    return mcp


def _presented_token(scope) -> str:
    headers = dict(scope.get("headers") or [])
    token = headers.get(b"x-mcp-token", b"").decode().strip()
    if token:
        return token
    auth = headers.get(b"authorization", b"").decode().strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


class TokenAuthMiddleware:
    """Require the configured static token for every HTTP request."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if not self.token:
                await JSONResponse(
                    {"error": "MCP_STATIC_TOKEN not configured"}, status_code=503
                )(scope, receive, send)
                return
            presented = _presented_token(scope)
            if not hmac.compare_digest(presented.encode("utf-8"), self.token.encode("utf-8")):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def build_http_app(mode: str | None = None):
    selected_mode = (mode if mode is not None else os.environ.get("MCP_MODE", "")).strip().lower()
    if selected_mode == "static":
        mcp = build_static_mcp()
        return TokenAuthMiddleware(
            mcp.http_app(path="/mcp"), os.environ.get("MCP_STATIC_TOKEN", "").strip()
        )
    if selected_mode == "oauth":
        return build_oauth_mcp().http_app(path="/mcp")
    raise RuntimeError("MCP_MODE must be set to 'static' or 'oauth'")

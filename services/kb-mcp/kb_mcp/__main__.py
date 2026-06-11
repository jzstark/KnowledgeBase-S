import hmac
import os

import uvicorn
from starlette.responses import JSONResponse

from .server import mcp

# Inbound auth for the public MCP endpoint. The KB API itself is protected by
# KB_SERVICE_TOKEN on the outbound side (server.py); this guards who may reach
# the MCP server at all. Fail closed: a blank token rejects every request so a
# misconfigured deploy never exposes the knowledge base.
MCP_STATIC_TOKEN = os.environ.get("MCP_STATIC_TOKEN", "").strip()


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
    """Reject HTTP requests that don't present MCP_STATIC_TOKEN.

    Lifespan and other non-HTTP scopes pass straight through so the streamable
    HTTP session manager starts normally.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if not MCP_STATIC_TOKEN:
                await JSONResponse(
                    {"error": "MCP_STATIC_TOKEN not configured"}, status_code=503
                )(scope, receive, send)
                return
            presented = _presented_token(scope)
            if not hmac.compare_digest(
                presented.encode("utf-8"), MCP_STATIC_TOKEN.encode("utf-8")
            ):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def main() -> None:
    port = int(os.environ.get("PORT", "7878"))
    mcp.settings.streamable_http_path = "/mcp"
    app = TokenAuthMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

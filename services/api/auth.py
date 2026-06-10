import os
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Header, HTTPException, status
from jose import JWTError, jwt

AUTH_PASSWORD = os.environ["AUTH_PASSWORD"]
AUTH_SECRET = os.environ["AUTH_SECRET"]
KB_SERVICE_TOKEN = os.environ.get("KB_SERVICE_TOKEN", "").strip()
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7


def verify_password(password: str) -> bool:
    return password.strip() == AUTH_PASSWORD


def create_token() -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode({"sub": "user", "exp": expire}, AUTH_SECRET, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, AUTH_SECRET, algorithms=[ALGORITHM])
        if payload.get("sub") != "user":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def require_auth(token: str | None = Cookie(default=None)) -> dict:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return verify_token(token)


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str) or not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def verify_service_token(token: str | None) -> dict | None:
    if not KB_SERVICE_TOKEN or not isinstance(token, str) or not token:
        return None
    if hmac.compare_digest(token, KB_SERVICE_TOKEN):
        # A single trusted-internal credential, shared by the kb-chat MCP adapter
        # (read-only) and the ingestion worker (which legitimately writes during
        # ingest). It is NOT a least-privilege scope: access is bounded by *which
        # dependency* an endpoint uses, not by this value (see below).
        return {"sub": "service", "scope": "service"}
    return None


def require_auth_or_service_token(
    token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    x_kb_service_token: str | None = Header(default=None),
) -> dict:
    """Accept a logged-in user (cookie) OR the trusted service token.

    This is the trust boundary: endpoints safe for the service token (reads and
    the worker's ingest path) use this; user-only/destructive endpoints (delete,
    merge, settings, …) use ``require_auth`` so the service token cannot reach
    them. Do not attach this to a new destructive endpoint.
    """
    if isinstance(token, str) and token:
        return verify_token(token)

    service_identity = verify_service_token(x_kb_service_token) or verify_service_token(
        _extract_bearer_token(authorization)
    )
    if service_identity:
        return service_identity

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

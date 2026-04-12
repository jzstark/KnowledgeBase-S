import os
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, HTTPException, status
from jose import JWTError, jwt

AUTH_PASSWORD = os.environ["AUTH_PASSWORD"]
AUTH_SECRET = os.environ["AUTH_SECRET"]
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 7


def verify_password(password: str) -> bool:
    return password == AUTH_PASSWORD


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

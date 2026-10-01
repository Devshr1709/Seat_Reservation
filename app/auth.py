from fastapi import Depends, Header, HTTPException

from .config import ADMIN_TOKEN, TOKEN_RE


def token(authorization: str | None = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    t = authorization[7:].strip()
    if not TOKEN_RE.match(t):
        raise HTTPException(401, "invalid token")
    return t


def admin(t: str = Depends(token)) -> str:
    if t != ADMIN_TOKEN:
        raise HTTPException(403, "admin only")
    return t

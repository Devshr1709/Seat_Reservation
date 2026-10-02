from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import ADMIN_TOKEN, TOKEN_RE

bearer = HTTPBearer(auto_error=False)


def token(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if not creds:
        raise HTTPException(401, "missing bearer token")
    t = creds.credentials.strip()
    if not TOKEN_RE.match(t):
        raise HTTPException(401, "invalid token")
    return t


def admin(t: str = Depends(token)) -> str:
    if t != ADMIN_TOKEN:
        raise HTTPException(403, "admin only")
    return t

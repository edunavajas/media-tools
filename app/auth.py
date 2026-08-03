"""Bearer token authentication dependency.

Fail-closed: if MEDIA_TOOLS_TOKEN is not configured the service answers 500
instead of running unauthenticated. A wrong token answers 401.
"""
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config

_bearer = HTTPBearer(auto_error=False)


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    expected = config.media_tools_token()
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="MEDIA_TOOLS_TOKEN is not configured; service is fail-closed",
        )
    if credentials is None or credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")

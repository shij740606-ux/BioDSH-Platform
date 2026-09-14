import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from biodsh_platform.config import get_settings

bearer = HTTPBearer(auto_error=False)


def require_platform_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    expected = get_settings().platform_token
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid platform token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "platform"


PlatformActor = Annotated[str, Depends(require_platform_token)]

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from biodsh_platform.config import get_settings
from biodsh_platform.db import get_db
from biodsh_platform.models import ComputeNode, RefreshToken, User

bearer = HTTPBearer(auto_error=False)
password_hasher = PasswordHasher()
_DUMMY_HASH = password_hasher.hash("biodsh-timing-equalization-placeholder")


def normalize_username(username: str) -> str:
    return username.strip().casefold()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user: User) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": user.id,
        "ver": user.token_version,
        "type": "access",
        "jti": secrets.token_hex(16),
        "iat": now,
        "exp": expires,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    return token, settings.access_token_minutes * 60


def issue_refresh_token(db: Session, user: User) -> tuple[str, RefreshToken]:
    settings = get_settings()
    raw = secrets.token_urlsafe(48)
    record = RefreshToken(
        user_id=user.id,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
    )
    db.add(record)
    db.flush()
    return raw, record


def find_refresh_token(db: Session, raw: str) -> RefreshToken | None:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return db.scalar(select(RefreshToken).where(RefreshToken.token_hash == digest))


def hash_opaque_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_node_token() -> str:
    return secrets.token_urlsafe(48)


def require_node(
    db: Annotated[Session, Depends(get_db)],
    node_header_id: Annotated[str | None, Header(alias="X-BioDSH-Node-ID")] = None,
    node_token: Annotated[str | None, Header(alias="X-BioDSH-Node-Token")] = None,
) -> ComputeNode:
    node = db.get(ComputeNode, node_header_id) if node_header_id else None
    supplied_hash = hash_opaque_token(node_token or "")
    expected_hash = node.token_hash if node and node.token_hash else "0" * 64
    if node is None or not node_token or not secrets.compare_digest(supplied_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Missing or invalid node credentials")
    return node


def require_platform_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    expected = get_settings().platform_token.get_secret_value()
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bootstrap token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "bootstrap"


def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    if credentials is None:
        raise _unauthorized()
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "ver", "type", "jti", "iat", "exp", "iss", "aud"]},
        )
    except InvalidTokenError as exc:
        raise _unauthorized() from exc
    if payload.get("type") != "access":
        raise _unauthorized()
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active or user.token_version != payload.get("ver"):
        raise _unauthorized()
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator role required"
        )
    return user


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing, expired, or invalid access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


CurrentUser = Annotated[User, Depends(require_user)]
AdminUser = Annotated[User, Depends(require_admin)]
PlatformActor = Annotated[str, Depends(require_platform_token)]
NodeActor = Annotated[ComputeNode, Depends(require_node)]

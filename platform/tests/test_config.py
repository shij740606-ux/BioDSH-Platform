import pytest
from pydantic import ValidationError

from biodsh_platform.config import Settings


def test_production_rejects_sqlite() -> None:
    with pytest.raises(ValidationError, match="require PostgreSQL"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="sqlite:///unsafe.db",
            platform_token="x" * 64,
            jwt_secret="y" * 64,
        )


def test_production_rejects_short_token() -> None:
    with pytest.raises(ValidationError, match="32-character"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://user:pass@db/service",
            platform_token="x" * 16,
            jwt_secret="y" * 64,
        )


def test_production_rejects_automatic_schema_creation() -> None:
    with pytest.raises(ValidationError, match="Alembic migrations"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://user:pass@db/service",
            platform_token="x" * 64,
            jwt_secret="y" * 64,
            auto_create_schema=True,
        )


def test_api_prefix_is_normalized() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///development.db",
        platform_token="development-token-long-enough",
        jwt_secret="development-jwt-secret-long-enough-for-tests",
        api_prefix="api/v2/",
    )
    assert settings.api_prefix == "/api/v2"

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIODSH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    service_name: str = "biodsh-platform"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["critical", "error", "warning", "info", "debug", "trace"] = "info"
    api_prefix: str = "/api/v1"
    docs_enabled: bool = True

    database_url: str
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout: int = Field(default=30, ge=1, le=300)
    database_pool_recycle: int = Field(default=1800, ge=60)

    platform_token: SecretStr
    auto_create_schema: bool = False
    cors_origins: list[str] = Field(default_factory=list)

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        normalized = "/" + value.strip().strip("/")
        if normalized == "/":
            raise ValueError("api_prefix must not be empty")
        return normalized

    @model_validator(mode="after")
    def validate_environment_contract(self) -> "Settings":
        token = self.platform_token.get_secret_value()
        if len(token) < 16:
            raise ValueError("platform_token must contain at least 16 characters")
        if self.environment in {"staging", "production"}:
            if self.database_url.startswith("sqlite"):
                raise ValueError("staging and production require PostgreSQL")
            if len(token) < 32:
                raise ValueError("staging and production require a 32-character platform token")
            if self.auto_create_schema:
                raise ValueError("staging and production must use Alembic migrations")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

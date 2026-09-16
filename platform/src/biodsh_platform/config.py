from functools import lru_cache
from pathlib import Path
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
    jwt_secret: SecretStr
    jwt_issuer: str = "biodsh-platform"
    jwt_audience: str = "biodsh-desktop"
    access_token_minutes: int = Field(default=15, ge=5, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=90)
    login_max_attempts: int = Field(default=5, ge=3, le=20)
    login_lock_minutes: int = Field(default=15, ge=1, le=1440)
    node_offline_seconds: int = Field(default=45, ge=15, le=600)
    metrics_retention_days: int = Field(default=7, ge=1, le=90)
    job_lease_seconds: int = Field(default=45, ge=15, le=600)
    artifact_root: Path = Path("./biodsh-artifacts")
    artifact_max_bytes: int = Field(default=1_073_741_824, ge=1_048_576)
    auto_create_schema: bool = False
    project_node_authorization_enabled: bool = False
    feishu_notifications_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_redirect_uri: str = ""
    feishu_allowed_tenants: list[str] = Field(default_factory=list)
    feishu_login_enabled: bool = False
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
        if self.feishu_notifications_enabled and not (
            self.feishu_app_id and self.feishu_app_secret.get_secret_value()
        ):
            raise ValueError("Feishu notifications require app credentials")
        if self.feishu_login_enabled:
            if not (
                self.feishu_app_id
                and self.feishu_app_secret.get_secret_value()
                and self.feishu_redirect_uri
                and self.feishu_allowed_tenants
            ):
                raise ValueError(
                    "Feishu login requires app credentials, redirect URI and allowed tenants"
                )
            if not self.feishu_redirect_uri.startswith("https://") and self.environment not in {
                "development",
                "test",
            }:
                raise ValueError("Feishu production callback requires HTTPS")
        token = self.platform_token.get_secret_value()
        jwt_secret = self.jwt_secret.get_secret_value()
        if len(token) < 16:
            raise ValueError("platform_token must contain at least 16 characters")
        if len(jwt_secret) < 32:
            raise ValueError("jwt_secret must contain at least 32 characters")
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

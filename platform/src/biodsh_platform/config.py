from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIODSH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "sqlite:///./biodsh-dev.db"
    platform_token: str = Field(default="local-development-token", min_length=16)
    auto_create_schema: bool = True
    cors_origins: list[str] = ["tauri://localhost", "http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()

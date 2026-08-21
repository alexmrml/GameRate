from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "GameRate"
    app_env: str = "development"
    app_timezone: str = "Europe/Istanbul"
    database_url: str = "postgresql+psycopg://gamerate:gamerate@db:5432/gamerate"
    session_cookie_name: str = "gamerate_session"
    session_ttl_hours: int = Field(default=24 * 7, ge=1)
    cookie_secure: bool = False
    worker_id: str = "worker-1"
    worker_poll_seconds: float = Field(default=2.0, ge=0.2)
    worker_stale_seconds: int = Field(default=30, ge=5)

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

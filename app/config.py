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

    # Metacritic collection. Metacritic renders server-side, so plain HTTP is enough.
    metacritic_base_url: str = "https://www.metacritic.com"
    metacritic_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    crawl_batch_size: int = Field(default=20, ge=1, le=100)
    crawl_request_delay_seconds: float = Field(default=0.6, ge=0.0)
    crawl_request_timeout_seconds: float = Field(default=30.0, gt=0)
    crawl_max_retries: int = Field(default=3, ge=1, le=10)
    crawl_max_platforms: int = Field(default=8, ge=1, le=30)
    crawl_critic_review_pages: int = Field(default=2, ge=1, le=20)
    crawl_user_reviews_per_platform: int = Field(default=25, ge=0, le=200)
    crawl_max_browse_pages_per_run: int = Field(default=6, ge=1, le=50)
    schedule_interval_minutes: int = Field(default=60, ge=1)
    run_stale_seconds: int = Field(default=900, ge=60)

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

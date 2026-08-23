from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.youtube_proxies import parse_proxy_list


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

    # Gemini enrichment. The key is environment-owned and never editable from /settings.
    gemini_api_key: str = ""
    # Free keys allow ~5 requests/minute on gemini-*-flash but far more on Gemma,
    # which the hourly crawler needs; switch models from /settings on a paid key.
    gemini_model: str = "gemma-4-31b-it"
    gemini_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    gemini_max_retries: int = Field(default=3, ge=1, le=10)
    # A generous ceiling on one HTTP request, not a latency target: without it the SDK
    # waits forever and a stalled connection freezes the whole run. A request that hits
    # this bound is not retried — the run moves on to the next game.
    gemini_request_timeout_seconds: float = Field(default=600.0, gt=0)
    # Three attempts are made inside one logical call. Temporary and schema failures wait
    # at least 30 seconds before the next attempt so a rate-limit window can actually move.
    gemini_retry_delay_seconds: float = Field(default=30.0, ge=30.0)
    # Free Gemini keys are limited per minute; pacing keeps a batch from tripping the quota.
    gemini_requests_per_minute: int = Field(default=25, ge=1, le=600)
    ai_enabled: bool = True
    ai_min_reviews: int = Field(default=3, ge=1)
    ai_max_reviews_per_audience: int = Field(default=40, ge=1)
    ai_max_games_per_run: int = Field(default=20, ge=0)
    ai_refresh_min_new_reviews: int = Field(default=5, ge=1)
    ai_refresh_min_growth: float = Field(default=0.25, ge=0.0)
    ai_min_refresh_interval_hours: int = Field(default=12, ge=0)
    similar_games_limit: int = Field(default=6, ge=1, le=24)

    # YouTube discovery has its own key and failure domain. Both API keys stay
    # environment-owned; only the behavioural knobs below may be overridden in DB.
    google_cloud_api_key: str = ""
    youtube_analysis_enabled: bool = True
    # The main path reads captions with yt-dlp and analyses text, so the cheap high-quota
    # Gemma model is enough; the multimodal model is only the fallback for videos that
    # publish no usable captions at all.
    youtube_analysis_model: str = "gemma-4-31b-it"
    youtube_video_fallback_model: str = "gemini-3.5-flash"
    youtube_analysis_fragment_minutes: int = Field(default=15, ge=1, le=60)
    # Measured let's-play tails run 40-100 spoken words a minute (Russian sits at the low
    # end). The bar is deliberately far below that: it only has to reject the anomaly —
    # credits, menus, outro music, an idle camera — not rank ordinary quiet commentary.
    youtube_transcript_min_words_per_minute: int = Field(default=15, ge=1, le=400)
    youtube_transcript_timeout_seconds: float = Field(default=60.0, gt=0)
    youtube_analysis_max_games_per_run: int = Field(default=5, ge=0, le=50)
    youtube_max_video_fallbacks_per_run: int = Field(default=1, ge=0, le=10)
    # Searching costs nothing now that yt-dlp serves it, and one search returns the same
    # page whether we read 5 rows or 50, so read the depth the filter wants.
    youtube_search_max_results: int = Field(default=50, ge=5, le=100)
    youtube_retry_interval_hours: int = Field(default=24, ge=1)
    youtube_no_result_refresh_days: int = Field(default=30, ge=1)
    # Comma- or newline-separated URLs. Credentials remain environment-owned unless an
    # administrator explicitly adds a proxy through the authenticated settings page.
    youtube_proxies: str = ""

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value

    @field_validator("youtube_proxies")
    @classmethod
    def validate_youtube_proxies(cls, value: str) -> str:
        parse_proxy_list(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

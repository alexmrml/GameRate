import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


class RunTrigger(enum.StrEnum):
    MANUAL = "manual"
    DAILY = "daily"
    SCHEDULED = "scheduled"


class RunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Audience(enum.StrEnum):
    CRITICS = "critics"
    USERS = "users"


class CrawlStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


game_genres = Table(
    "game_genres",
    Base.metadata,
    Column("game_id", ForeignKey("games.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)

game_tags = Table(
    "game_tags",
    Base.metadata,
    Column("game_id", ForeignKey("games.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class Game(Base):
    __tablename__ = "games"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str | None] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    release_date: Mapped[date | None] = mapped_column(Date)
    developer: Mapped[str | None] = mapped_column(String(255))
    publisher: Mapped[str | None] = mapped_column(String(255))
    metacritic_url: Mapped[str | None] = mapped_column(String(1000))
    cover_image_url: Mapped[str | None] = mapped_column(String(1000))
    video_url: Mapped[str | None] = mapped_column(String(1000))
    esrb_rating: Mapped[str | None] = mapped_column(String(16))
    # Metacritic's own genre-peer carousel, kept as slugs for similarity matching.
    related_slugs: Mapped[list[str] | None] = mapped_column(JSON)
    ai_tags_digest: Mapped[str | None] = mapped_column(String(64))
    ai_tags_model: Mapped[str | None] = mapped_column(String(120))
    ai_tags_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    platforms: Mapped[list["GamePlatform"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    genres: Mapped[list["Genre"]] = relationship(secondary=game_genres, back_populates="games")
    tags: Mapped[list["Tag"]] = relationship(secondary=game_tags, back_populates="games")
    reviews: Mapped[list["GameReview"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    review_summaries: Mapped[list["ReviewSummary"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    youtube_analysis: Mapped["YouTubeAnalysis | None"] = relationship(
        back_populates="game", cascade="all, delete-orphan", uselist=False
    )


class Platform(Base):
    __tablename__ = "platforms"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    games: Mapped[list["GamePlatform"]] = relationship(back_populates="platform")


class GamePlatform(Base):
    __tablename__ = "game_platforms"
    __table_args__ = (
        UniqueConstraint("game_id", "platform_id", name="uq_game_platform"),
        CheckConstraint("metascore IS NULL OR metascore BETWEEN 0 AND 100", name="ck_metascore"),
        CheckConstraint("userscore IS NULL OR userscore BETWEEN 0 AND 10", name="ck_userscore"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    platform_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platforms.id", ondelete="RESTRICT"))
    metascore: Mapped[int | None] = mapped_column(SmallInteger)
    userscore: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    critic_review_count: Mapped[int | None] = mapped_column(Integer)
    user_rating_count: Mapped[int | None] = mapped_column(Integer)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    last_scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    game: Mapped[Game] = relationship(back_populates="platforms")
    platform: Mapped[Platform] = relationship(back_populates="games")


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    games: Mapped[list[Game]] = relationship(secondary=game_genres, back_populates="genres")


class Tag(Base):
    """A similarity facet value. `facet` groups tags so matching can weight them apart."""

    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    facet: Mapped[str | None] = mapped_column(String(40), index=True)
    games: Mapped[list[Game]] = relationship(secondary=game_tags, back_populates="tags")


class GameReview(Base):
    """A single collected review kept verbatim as summarization input."""

    __tablename__ = "game_reviews"
    __table_args__ = (
        UniqueConstraint("game_id", "external_key", name="uq_game_review_external_key"),
        Index("ix_game_reviews_game_audience", "game_id", "audience"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    platform_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("platforms.id", ondelete="SET NULL")
    )
    audience: Mapped[Audience] = mapped_column(Enum(Audience, name="review_audience"))
    external_key: Mapped[str] = mapped_column(String(255))
    quote: Mapped[str] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    author: Mapped[str | None] = mapped_column(String(255))
    publication: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1000))
    review_date: Mapped[date | None] = mapped_column(Date)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    game: Mapped[Game] = relationship(back_populates="reviews")
    platform: Mapped[Platform | None] = relationship()


class ReviewSummary(Base):
    __tablename__ = "review_summaries"
    __table_args__ = (
        UniqueConstraint("game_id", "platform_id", "audience", name="uq_review_summary_scope"),
        Index(
            "uq_review_summary_game_audience",
            "game_id",
            "audience",
            unique=True,
            postgresql_where=text("platform_id IS NULL"),
            sqlite_where=text("platform_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    platform_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("platforms.id", ondelete="SET NULL")
    )
    audience: Mapped[Audience] = mapped_column(Enum(Audience, name="review_audience"))
    summary: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(200))
    positives: Mapped[list[str] | None] = mapped_column(JSON)
    negatives: Mapped[list[str] | None] = mapped_column(JSON)
    # "<prompt version>:<sha256 of the review keys>", so a prompt change is visible.
    input_digest: Mapped[str | None] = mapped_column(String(80))
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    model_name: Mapped[str | None] = mapped_column(String(255))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    game: Mapped[Game] = relationship(back_populates="review_summaries")
    platform: Mapped[Platform | None] = relationship()


class YouTubeAnalysis(Base):
    __tablename__ = "youtube_analyses"
    __table_args__ = (UniqueConstraint("game_id", name="uq_youtube_analysis_game"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(40), index=True)
    status_reason: Mapped[str | None] = mapped_column(String(1000))
    search_query: Mapped[str | None] = mapped_column(String(1000))
    search_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    search_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    video_id: Mapped[str | None] = mapped_column(String(32), index=True)
    video_url: Mapped[str | None] = mapped_column(String(1000))
    video_title: Mapped[str | None] = mapped_column(String(500))
    channel_id: Mapped[str | None] = mapped_column(String(80))
    channel_title: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    view_count: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    fragment_start_seconds: Mapped[int | None] = mapped_column(Integer)
    fragment_end_seconds: Mapped[int | None] = mapped_column(Integer)
    speech_transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    liked: Mapped[list[str] | None] = mapped_column(JSON)
    disliked: Mapped[list[str] | None] = mapped_column(JSON)
    analysis_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_name: Mapped[str | None] = mapped_column(String(255))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    game: Mapped[Game] = relationship(back_populates="youtube_analysis")


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        Index(
            "uq_single_running_processing_run",
            "status",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
            sqlite_where=text("status = 'RUNNING'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    trigger: Mapped[RunTrigger] = mapped_column(Enum(RunTrigger, name="run_trigger"))
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.QUEUED, index=True
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    worker_id: Mapped[str | None] = mapped_column(String(255), index=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    current_game_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("games.id", ondelete="SET NULL")
    )
    message: Mapped[str | None] = mapped_column(String(1000))
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    requested_by: Mapped[User | None] = relationship()
    current_game: Mapped[Game | None] = relationship()


class DailyCrawlState(Base):
    __tablename__ = "daily_crawl_states"

    id: Mapped[uuid.UUID] = uuid_pk()
    processing_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    status: Mapped[CrawlStatus] = mapped_column(Enum(CrawlStatus, name="crawl_status"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="SET NULL")
    )
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DailyProcessedGame(Base):
    __tablename__ = "daily_processed_games"
    __table_args__ = (
        UniqueConstraint("processing_date", "game_id", name="uq_daily_processed_game"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    processing_date: Mapped[date] = mapped_column(Date, index=True)
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="SET NULL")
    )
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, name="daily_game_status"))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    current_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(String(500))
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

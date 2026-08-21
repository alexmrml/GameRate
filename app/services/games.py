import re
import unicodedata
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.metacritic import (
    CRITIC_AUDIENCE,
    GameSnapshot,
    PlatformScore,
    ReviewRecord,
)
from app.models import Audience, Game, GamePlatform, GameReview, Genre, Platform
from app.time import utc_now


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def source_key_for(title: str, release_date: date | None, external_key: str | None) -> str:
    if external_key:
        return f"metacritic:{external_key.strip().lower()}"
    year = release_date.year if release_date else "unknown"
    return f"fallback:{normalize_title(title)}:{year}"


def upsert_discovered_game(
    db: Session,
    *,
    title: str,
    release_date: date | None = None,
    external_key: str | None = None,
    **fields: Any,
) -> Game:
    """Create or refresh a game using its stable discovery identity."""
    key = source_key_for(title, release_date, external_key)
    game = db.scalar(select(Game).where(Game.source_key == key))
    now = utc_now()
    if game is None:
        game = Game(
            source_key=key,
            title=title,
            release_date=release_date,
            discovered_at=now,
            last_discovered_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(game)
    else:
        game.title = title
        if release_date is not None:
            game.release_date = release_date
        game.last_discovered_at = now
        game.updated_at = now
    for name, value in fields.items():
        if hasattr(game, name):
            setattr(game, name, value)
    db.flush()
    return game


def get_or_create_platform(db: Session, *, slug: str, name: str) -> Platform:
    platform = db.scalar(select(Platform).where(Platform.slug == slug))
    if platform is None:
        platform = db.scalar(select(Platform).where(Platform.name == name))
    if platform is None:
        platform = Platform(slug=slug, name=name, created_at=utc_now())
        db.add(platform)
        db.flush()
    return platform


def get_or_create_genre(db: Session, name: str) -> Genre:
    slug = normalize_title(name)
    genre = db.scalar(select(Genre).where(Genre.slug == slug))
    if genre is None:
        genre = Genre(slug=slug, name=name)
        db.add(genre)
        db.flush()
    return genre


def _apply_platform(db: Session, game: Game, score: PlatformScore) -> Platform:
    platform = get_or_create_platform(db, slug=score.slug, name=score.name)
    now = utc_now()
    row = db.scalar(
        select(GamePlatform).where(
            GamePlatform.game_id == game.id, GamePlatform.platform_id == platform.id
        )
    )
    if row is None:
        row = GamePlatform(game_id=game.id, platform_id=platform.id, created_at=now, updated_at=now)
        db.add(row)
    row.metascore = score.metascore
    row.userscore = score.userscore
    row.critic_review_count = score.critic_review_count
    row.user_rating_count = score.user_rating_count
    row.source_url = score.source_url
    row.updated_at = now
    if score.metascore is not None or score.userscore is not None:
        row.last_scored_at = now
    db.flush()
    return platform


def _apply_review(
    db: Session, game: Game, review: ReviewRecord, platforms: dict[str, Platform]
) -> None:
    now = utc_now()
    audience = Audience.CRITICS if review.audience == CRITIC_AUDIENCE else Audience.USERS
    platform = platforms.get(review.platform_slug) if review.platform_slug else None
    row = db.scalar(
        select(GameReview).where(
            GameReview.game_id == game.id, GameReview.external_key == review.external_key
        )
    )
    if row is None:
        row = GameReview(
            game_id=game.id,
            external_key=review.external_key,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    row.platform_id = platform.id if platform else None
    row.audience = audience
    row.quote = review.quote
    row.score = review.score
    row.author = review.author
    row.publication = review.publication
    row.url = review.url
    row.review_date = review.review_date
    row.collected_at = now
    row.updated_at = now


def apply_game_snapshot(db: Session, snapshot: GameSnapshot) -> Game:
    """Persist one collected Metacritic game, refreshing it when already known."""
    game = upsert_discovered_game(
        db,
        title=snapshot.title,
        release_date=snapshot.release_date,
        external_key=snapshot.slug,
        slug=snapshot.slug,
        description=snapshot.description,
        developer=snapshot.developer,
        publisher=snapshot.publisher,
        metacritic_url=snapshot.metacritic_url,
        cover_image_url=snapshot.cover_image_url,
        video_url=snapshot.video_url,
        esrb_rating=snapshot.esrb_rating,
        related_slugs=snapshot.related_slugs or None,
    )

    platforms = {score.slug: _apply_platform(db, game, score) for score in snapshot.platforms}

    genres = [get_or_create_genre(db, name) for name in snapshot.genres]
    existing_genre_ids = {genre.id for genre in game.genres}
    for genre in genres:
        if genre.id not in existing_genre_ids:
            game.genres.append(genre)

    for review in snapshot.reviews:
        _apply_review(db, game, review, platforms)

    db.flush()
    return game

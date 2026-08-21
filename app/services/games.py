import re
import unicodedata
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game
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
        game.release_date = release_date
        game.last_discovered_at = now
        game.updated_at = now
    for name, value in fields.items():
        if hasattr(game, name):
            setattr(game, name, value)
    db.flush()
    return game

import os
from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["APP_TIMEZONE"] = "Europe/Istanbul"
# Pin collection settings so a developer's local .env cannot change test outcomes.
os.environ["CRAWL_BATCH_SIZE"] = "20"
os.environ["CRAWL_REQUEST_DELAY_SECONDS"] = "0"
os.environ["CRAWL_MAX_BROWSE_PAGES_PER_RUN"] = "6"
os.environ["CRAWL_CRITIC_REVIEW_PAGES"] = "2"
os.environ["CRAWL_USER_REVIEWS_PER_PLATFORM"] = "25"
os.environ["CRAWL_MAX_PLATFORMS"] = "8"
os.environ["SCHEDULE_INTERVAL_MINUTES"] = "60"
os.environ["RUN_STALE_SECONDS"] = "900"

from app.collectors.metacritic import (  # noqa: E402
    CRITIC_AUDIENCE,
    USER_AUDIENCE,
    GameSnapshot,
    MetacriticError,
    PlatformScore,
    ReviewRecord,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.time import utc_now  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "metacritic"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def build_snapshot(
    slug: str,
    *,
    title: str | None = None,
    metascore: int | None = 80,
    userscore: str | None = "7.5",
    platforms: tuple[str, ...] = ("pc",),
    reviews: int = 2,
) -> GameSnapshot:
    """A collected game shaped like the collector's output, without any HTTP."""
    snapshot = GameSnapshot(
        slug=slug,
        title=title or slug.replace("-", " ").title(),
        metacritic_url=f"https://www.metacritic.com/game/{slug}/",
        description=f"Description of {slug}",
        developer="Test Studio",
        publisher="Test Publisher",
        release_date=date(2026, 8, 1),
        cover_image_url=f"https://www.metacritic.com/a/img/catalog/{slug}.jpg",
        video_url=f"https://cdn.example.com/{slug}.html",
        genres=["Action"],
    )
    for name in platforms:
        snapshot.platforms.append(
            PlatformScore(
                slug=name,
                name=name.upper(),
                metascore=metascore,
                userscore=Decimal(userscore) if userscore is not None else None,
                critic_review_count=reviews,
                user_rating_count=reviews,
                source_url=f"https://www.metacritic.com/game/{slug}/critic-reviews/?platform={name}",
            )
        )
    for index in range(reviews):
        snapshot.reviews.append(
            ReviewRecord(
                audience=CRITIC_AUDIENCE,
                external_key=f"critic:{platforms[0]}:{slug}-{index}",
                quote=f"Critic opinion {index} about {slug}",
                platform_slug=platforms[0],
                score=Decimal(85),
                publication=f"Publication {index}",
                url=f"https://example.com/{slug}/{index}",
                review_date=date(2026, 8, 2),
            )
        )
        snapshot.reviews.append(
            ReviewRecord(
                audience=USER_AUDIENCE,
                external_key=f"user:{platforms[0]}:{slug}-{index}",
                quote=f"Player opinion {index} about {slug}",
                platform_slug=platforms[0],
                score=Decimal(7),
                author=f"player{index}",
                review_date=date(2026, 8, 3),
            )
        )
    return snapshot


class StubMetacriticClient:
    """In-memory stand-in for the collector used by crawl and pipeline tests."""

    def __init__(
        self,
        *,
        new_releases: list[str] | None = None,
        browse_pages: dict[int, list[str]] | None = None,
        failing_slugs: set[str] | None = None,
        discovery_error: str | None = None,
        snapshots: dict[str, GameSnapshot] | None = None,
    ) -> None:
        self.new_releases = new_releases or []
        self.browse_pages = browse_pages or {}
        self.failing_slugs = failing_slugs or set()
        self.discovery_error = discovery_error
        self.snapshots = snapshots or {}
        self.collected: list[str] = []
        self.requested_pages: list[int] = []
        self.closed = False

    def new_release_slugs(self) -> list[str]:
        if self.discovery_error:
            raise MetacriticError(self.discovery_error)
        return list(self.new_releases)

    def browse_slugs(self, page: int) -> list[str]:
        if self.discovery_error:
            raise MetacriticError(self.discovery_error)
        self.requested_pages.append(page)
        return list(self.browse_pages.get(page, []))

    def collect_game(self, slug: str) -> GameSnapshot:
        self.collected.append(slug)
        if slug in self.failing_slugs:
            raise MetacriticError(f"GET /game/{slug}/ returned 503")
        return self.snapshots.get(slug) or build_snapshot(slug)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def user() -> User:
    now = utc_now()
    with SessionLocal() as db:
        user = User(
            username="admin",
            password_hash=hash_password("correct-horse-battery"),
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(client: TestClient, user: User) -> TestClient:
    response = client.post(
        "/login",
        data={
            "username": user.username,
            "password": "correct-horse-battery",
            "next": "/games",
        },
    )
    assert response.status_code == 303
    return client

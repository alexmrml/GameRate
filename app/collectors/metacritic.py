"""Metacritic collector.

Everything here is read-only and database-free: it turns Metacritic pages into plain
dataclasses that the service layer persists. Metacritic renders server-side, so plain
HTTP is enough and no browser automation is involved.
"""

import hashlib
import logging
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.collectors.nuxt import components, find_data_entry, payload_data
from app.config import settings

logger = logging.getLogger("gamerate.collectors.metacritic")

NEW_RELEASES_PATH = "/game/"
BROWSE_PATH = "/browse/game/all/all/all-time/new/"
CRITIC_AUDIENCE = "critics"
USER_AUDIENCE = "users"
CRITIC_REVIEWS_PER_PAGE = 10
MAX_RELATED_SLUGS = 24

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class MetacriticError(RuntimeError):
    """Metacritic could not be read. Never swallowed into empty results."""


class MetacriticNotFound(MetacriticError):
    """The requested Metacritic document does not exist."""


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


@dataclass(slots=True)
class PlatformScore:
    slug: str
    name: str
    metascore: int | None = None
    userscore: Decimal | None = None
    critic_review_count: int | None = None
    user_rating_count: int | None = None
    source_url: str | None = None


@dataclass(slots=True)
class ReviewRecord:
    audience: str
    external_key: str
    quote: str
    platform_slug: str | None = None
    score: Decimal | None = None
    author: str | None = None
    publication: str | None = None
    url: str | None = None
    review_date: date | None = None


@dataclass(slots=True)
class GameSnapshot:
    slug: str
    title: str
    metacritic_url: str
    description: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release_date: date | None = None
    cover_image_url: str | None = None
    video_url: str | None = None
    esrb_rating: str | None = None
    # Metacritic's own "more like this" carousel: its genre peers, ranked by score.
    related_slugs: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    platforms: list[PlatformScore] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)

    def review_count(self, audience: str) -> int:
        return sum(1 for review in self.reviews if review.audience == audience)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _as_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _absolute(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    return f"{settings.metacritic_base_url.rstrip('/')}{url}"


def _rows(component: Any) -> list[dict[str, Any]]:
    """Review components expose their rows under ``items`` or ``item``."""
    if not isinstance(component, dict):
        return []
    for key in ("items", "item"):
        value = component.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _company(item: dict[str, Any], type_name: str) -> str | None:
    production = item.get("production")
    if not isinstance(production, dict):
        return None
    for company in production.get("companies") or []:
        if isinstance(company, dict) and company.get("typeName") == type_name:
            name = _clean(company.get("name"))
            if name:
                return name
    return None


def _image_url(item: dict[str, Any], type_name: str) -> str | None:
    for image in item.get("images") or []:
        if not isinstance(image, dict) or image.get("typeName") != type_name:
            continue
        bucket_type = _clean(image.get("bucketType"))
        bucket_path = _clean(image.get("bucketPath"))
        if bucket_type and bucket_path:
            return f"{settings.metacritic_base_url.rstrip('/')}/a/img/{bucket_type}{bucket_path}"
        direct = _clean(image.get("imageUrl"))
        if direct:
            return _absolute(direct)
    return None


def _video_url(item: dict[str, Any]) -> str | None:
    video = item.get("video")
    if not isinstance(video, dict):
        return None
    for key in ("embedUrl", "url", "manifestUrl"):
        url = _clean(video.get(key))
        if url:
            return _absolute(url)
    return None


def _slugs(items: Iterable[Any]) -> list[str]:
    slugs: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        slug = _clean(entry.get("slug"))
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def parse_new_releases(html: str) -> list[str]:
    """Slugs of the New Releases carousel on the games front door."""
    page = find_data_entry(payload_data(html), "loadPage:door:games-door")
    slugs = _slugs(_rows(components(page).get("new-releases-carousel")))
    if not slugs:
        raise MetacriticError("New Releases carousel carried no games")
    return slugs


def parse_browse_page(html: str) -> list[str]:
    """Slugs of one browse listing page, in the order Metacritic returned them."""
    entry = find_data_entry(payload_data(html), "browse-game-")
    if not isinstance(entry, dict):
        raise MetacriticError("Browse listing payload is not an object")
    return _slugs(entry.get("items") or [])


def parse_game_page(html: str) -> GameSnapshot:
    """Core metadata and per-platform Metascores for one game."""
    page = find_data_entry(payload_data(html), "loadPage:games:")
    page_components = components(page)
    product = page_components.get("product")
    item = product.get("item") if isinstance(product, dict) else None
    if not isinstance(item, dict):
        raise MetacriticError("Game page carried no product item")

    slug = _clean(item.get("slug"))
    title = _clean(item.get("title"))
    if not slug or not title:
        raise MetacriticError("Game page is missing its slug or title")

    platforms: list[PlatformScore] = []
    for entry in item.get("platforms") or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        if not name:
            continue
        summary = entry.get("criticScoreSummary")
        summary = summary if isinstance(summary, dict) else {}
        platforms.append(
            PlatformScore(
                slug=_clean(entry.get("slug")) or slugify(name),
                name=name,
                metascore=_as_int(summary.get("score")),
                critic_review_count=_as_int(summary.get("reviewCount")),
                source_url=_absolute(_clean(summary.get("url"))),
            )
        )

    genres: list[str] = []
    for genre in item.get("genres") or []:
        name = _clean(genre.get("name")) if isinstance(genre, dict) else None
        if name and name not in genres:
            genres.append(name)

    related = [
        other for other in _slugs(_rows(page_components.get("related-carousel"))) if other != slug
    ][:MAX_RELATED_SLUGS]

    return GameSnapshot(
        slug=slug,
        title=title,
        metacritic_url=f"{settings.metacritic_base_url.rstrip('/')}/game/{slug}/",
        description=_clean(item.get("description")),
        developer=_company(item, "Developer"),
        publisher=_company(item, "Publisher"),
        release_date=_as_date(item.get("releaseDate")),
        cover_image_url=_image_url(item, "cardImage") or _image_url(item, "mainImage"),
        video_url=_video_url(item),
        esrb_rating=_clean(item.get("rating")),
        related_slugs=related,
        genres=genres,
        platforms=platforms,
    )


def _platform_slug(row: dict[str, Any], known: dict[str, str]) -> str | None:
    name = _clean(row.get("platform"))
    if not name:
        product = row.get("reviewedProduct")
        platform = product.get("platform") if isinstance(product, dict) else None
        if isinstance(platform, dict):
            name = _clean(platform.get("name"))
    if not name:
        return None
    return known.get(name.casefold()) or slugify(name)


def _digest(*parts: str | None) -> str:
    payload = "|".join(part or "" for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def parse_critic_reviews(html: str, platform_names: dict[str, str]) -> list[ReviewRecord]:
    page = find_data_entry(payload_data(html), "loadPage:games-critic-reviews:")
    reviews: list[ReviewRecord] = []
    for row in _rows(components(page).get("critic-reviews")):
        quote = _clean(row.get("quote"))
        if not quote:
            continue
        platform_slug = _platform_slug(row, platform_names)
        url = _clean(row.get("url"))
        publication = _clean(row.get("publicationName"))
        digest = _digest(url, publication, _clean(row.get("date")))
        reviews.append(
            ReviewRecord(
                audience=CRITIC_AUDIENCE,
                external_key=f"critic:{platform_slug or 'all'}:{digest}",
                quote=quote,
                platform_slug=platform_slug,
                score=_as_decimal(row.get("score")),
                author=_clean(row.get("author")),
                publication=publication,
                url=url,
                review_date=_as_date(row.get("date")),
            )
        )
    return reviews


def parse_user_reviews(
    html: str, platform_names: dict[str, str]
) -> tuple[Decimal | None, int | None, list[ReviewRecord]]:
    """Return the userscore, rating count and review rows of one user-reviews page."""
    page = find_data_entry(payload_data(html), "loadPage:games-user-reviews:")
    page_components = components(page)

    summary = page_components.get("user-score-summary")
    summary_item = summary.get("item") if isinstance(summary, dict) else None
    summary_item = summary_item if isinstance(summary_item, dict) else {}

    reviews: list[ReviewRecord] = []
    for row in _rows(page_components.get("user-reviews")):
        quote = _clean(row.get("quote"))
        if not quote:
            continue
        platform_slug = _platform_slug(row, platform_names)
        identity = _clean(row.get("id")) or _digest(
            _clean(row.get("author")), _clean(row.get("date")), quote[:120]
        )
        reviews.append(
            ReviewRecord(
                audience=USER_AUDIENCE,
                external_key=f"user:{platform_slug or 'all'}:{identity}",
                quote=quote,
                platform_slug=platform_slug,
                score=_as_decimal(row.get("score")),
                author=_clean(row.get("author")),
                review_date=_as_date(row.get("date")),
            )
        )
    # Metacritic reports a "tbd" userscore as 0 with no sentiment. Storing that as a
    # real 0.0 rating would invent data, so it stays unknown until a score exists.
    userscore = _as_decimal(summary_item.get("score"))
    if userscore == 0 and not _clean(summary_item.get("sentiment")):
        userscore = None
    return userscore, _as_int(summary_item.get("reviewCount")), reviews


class MetacriticClient:
    """Throttled Metacritic reader.

    Failures surface as :class:`MetacriticError`; the collector never returns fabricated
    or empty data to hide a broken response.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str | None = None,
        delay_seconds: float | None = None,
        max_retries: int | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.base_url = (base_url or settings.metacritic_base_url).rstrip("/")
        self.delay_seconds = (
            settings.crawl_request_delay_seconds if delay_seconds is None else delay_seconds
        )
        self.max_retries = settings.crawl_max_retries if max_retries is None else max_retries
        self._sleep = sleep
        self._client = client or httpx.Client(
            timeout=settings.crawl_request_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": settings.metacritic_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._last_request_at: float | None = None

    def __enter__(self) -> "MetacriticClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        if self.delay_seconds <= 0:
            return
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.delay_seconds:
                self._sleep(self.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def fetch(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = MetacriticError(f"GET {url} failed: {exc}")
            else:
                if response.status_code == 404:
                    raise MetacriticNotFound(f"GET {url} returned 404")
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = MetacriticError(f"GET {url} returned {response.status_code}")
                elif response.is_error:
                    raise MetacriticError(f"GET {url} returned {response.status_code}")
                else:
                    return response.text
            if attempt < self.max_retries:
                logger.warning("retrying %s (attempt %s): %s", url, attempt, last_error)
                self._sleep(max(self.delay_seconds, 0.5) * (2**attempt))
        raise MetacriticError(str(last_error) if last_error else f"GET {url} failed")

    def new_release_slugs(self) -> list[str]:
        return parse_new_releases(self.fetch(NEW_RELEASES_PATH))

    def browse_slugs(self, page: int) -> list[str]:
        params = {"page": page} if page > 1 else None
        return parse_browse_page(self.fetch(BROWSE_PATH, params=params))

    def _game_path(self, slug: str, suffix: str = "") -> str:
        return f"/game/{quote(slug, safe='')}/{suffix}"

    def collect_game(self, slug: str) -> GameSnapshot:
        """Fetch one game plus the review material later stages summarize."""
        snapshot = parse_game_page(self.fetch(self._game_path(slug)))
        platform_names = {item.name.casefold(): item.slug for item in snapshot.platforms}

        for page in range(1, max(settings.crawl_critic_review_pages, 1) + 1):
            params = {"page": page} if page > 1 else None
            html = self.fetch(self._game_path(slug, "critic-reviews/"), params=params)
            page_reviews = parse_critic_reviews(html, platform_names)
            snapshot.reviews.extend(page_reviews)
            if len(page_reviews) < CRITIC_REVIEWS_PER_PAGE:
                break

        limit = max(settings.crawl_user_reviews_per_platform, 0)
        for platform in snapshot.platforms[: max(settings.crawl_max_platforms, 1)]:
            html = self.fetch(
                self._game_path(slug, "user-reviews/"), params={"platform": platform.slug}
            )
            userscore, rating_count, reviews = parse_user_reviews(html, platform_names)
            platform.userscore = userscore
            platform.user_rating_count = rating_count
            snapshot.reviews.extend(reviews[:limit])

        seen: set[str] = set()
        unique: list[ReviewRecord] = []
        for review in snapshot.reviews:
            if review.external_key in seen:
                continue
            seen.add(review.external_key)
            unique.append(review)
        snapshot.reviews = unique
        return snapshot

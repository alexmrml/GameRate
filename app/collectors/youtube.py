"""Read-only YouTube Data API discovery and deterministic candidate filtering."""

import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx

from app.config import settings

YOUTUBE_BASE_URL = "https://www.googleapis.com/youtube/v3"


class YouTubeError(RuntimeError):
    """Base class for YouTube Data API failures."""


class YouTubeUnavailable(YouTubeError):
    """Credentials, permissions, or API configuration make requests impossible."""


class YouTubeQuotaExceeded(YouTubeError):
    """The project's daily search or Data API quota is exhausted."""


class YouTubeTemporaryError(YouTubeError):
    """A transient transport or provider failure."""


@dataclass(slots=True)
class VideoCandidate:
    video_id: str
    url: str
    title: str
    channel_id: str | None
    channel_title: str | None
    published_at: datetime | None
    view_count: int
    duration_seconds: int
    description: str = ""
    rejection_reason: str | None = None

    def to_cache(self) -> dict[str, Any]:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        return data

    @classmethod
    def from_cache(cls, data: dict[str, Any]) -> "VideoCandidate":
        published = data.get("published_at")
        return cls(
            video_id=str(data["video_id"]),
            url=str(data["url"]),
            title=str(data["title"]),
            channel_id=data.get("channel_id"),
            channel_title=data.get("channel_title"),
            published_at=_parse_datetime(published) if published else None,
            view_count=int(data.get("view_count") or 0),
            duration_seconds=int(data.get("duration_seconds") or 0),
            description=str(data.get("description") or ""),
            rejection_reason=data.get("rejection_reason"),
        )


@dataclass(slots=True)
class YouTubeSearchResult:
    query: str
    candidates: list[VideoCandidate]

    def selected(self, excluded_video_ids: set[str] | None = None) -> VideoCandidate | None:
        excluded = excluded_video_ids or set()
        eligible = [
            item
            for item in self.candidates
            if item.rejection_reason is None and item.video_id not in excluded
        ]
        return max(eligible, key=lambda item: item.view_count, default=None)


_POSITIVE_TERMS = (
    "gameplay",
    "playthrough",
    "lets play",
    "walkthrough",
    "full game",
    "part 1",
    "episode 1",
    "livestream",
    "live stream",
    "stream vod",
    "прохожд",
    "летспле",
    "геймпле",
)

_REJECT_TERMS = (
    "trailer",
    "teaser",
    "review",
    "before you buy",
    "first impressions",
    "retrospective",
    "video essay",
    "guide",
    "tips and tricks",
    "how to",
    "tutorial",
    "build guide",
    "getting started",
    "walkthrough guide",
    "platinum walkthrough",
    "achievement walkthrough",
    "all collectibles",
    "boss fight",
    "boss battle",
    "soundtrack",
    " ost ",
    "music video",
    "cutscene",
    "all cutscenes",
    "game movie",
    "full movie",
    "cinematic",
    "ending",
    "lore",
    "explained",
    "benchmark",
    "graphics comparison",
    "reaction",
    "speedrun",
    "world record",
    "no commentary",
    "without commentary",
    "no talking",
    "без комментариев",
    "без комментария",
    "all deaths",
    "compilation",
    "gameplay reveal",
    "gameplay showcase",
    "gameplay demo",
    "official gameplay",
    "demo",
    "beta",
    "alpha gameplay",
    "early access",
    "preview build",
    "animation",
    "animated",
    "parody",
    "funny moments",
    "all bosses",
    "обзор",
    "гайд",
    "трейлер",
    "саундтрек",
    "катсцены",
    "первый взгляд",
)

_TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "edition",
    "for",
    "game",
    "of",
    "remake",
    "remastered",
    "the",
}


def build_search_query(title: str) -> str:
    clean_title = " ".join(title.replace('"', " ").split())
    terms = (
        "gameplay",
        "playthrough",
        "lets play",
        "walkthrough",
        "прохождение",
        "летсплей",
        "геймплей",
    )
    return "|".join(f'"{clean_title}" {term}' for term in terms)


def candidate_rejection_reason(candidate: VideoCandidate, game_title: str) -> str | None:
    """Reject obvious non-let's-play results without making subjective ranking calls."""
    normalized_title = _normalize(candidate.title)
    title_text = f" {normalized_title} "
    description_text = f" {_normalize(candidate.description[:1000])} "
    searchable = title_text + description_text

    if candidate.duration_seconds <= 180 or " shorts " in title_text or " short " in title_text:
        return "shorts_or_too_short"
    if not _matches_game(normalized_title, game_title):
        return "different_game"
    for term in _REJECT_TERMS:
        if f" {_normalize(term)} " in title_text:
            return f"excluded:{term.strip().replace(' ', '_')}"
    for term in ("no commentary", "without commentary", "no talking", "без комментариев"):
        if f" {_normalize(term)} " in description_text:
            return f"excluded:{term.replace(' ', '_')}"
    if not any(term in searchable for term in _POSITIVE_TERMS):
        return "no_gameplay_signal"
    return None


class YouTubeClient:
    """One search.list plus one batched videos.list for a game's complete candidate set."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.google_cloud_api_key
        if not self.api_key:
            raise YouTubeUnavailable("GOOGLE_CLOUD_API_KEY is not configured")
        self._client = client or httpx.Client(
            base_url=YOUTUBE_BASE_URL,
            timeout=settings.crawl_request_timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search_game(self, title: str) -> YouTubeSearchResult:
        """Search exactly once, then hydrate all results in one metadata request."""
        query = build_search_query(title)
        payload = self._get(
            "/search",
            params={
                "part": "snippet",
                "type": "video",
                "q": query,
                "order": "viewCount",
                "maxResults": settings.youtube_search_max_results,
                "videoEmbeddable": "true",
                "videoSyndicated": "true",
                "safeSearch": "none",
                "key": self.api_key,
            },
        )
        video_ids = list(
            dict.fromkeys(
                str(item.get("id", {}).get("videoId"))
                for item in payload.get("items", [])
                if item.get("id", {}).get("videoId")
            )
        )
        if not video_ids:
            return YouTubeSearchResult(query=query, candidates=[])

        metadata = self._get(
            "/videos",
            params={
                "part": "snippet,contentDetails,statistics,status",
                "id": ",".join(video_ids),
                "maxResults": len(video_ids),
                "key": self.api_key,
            },
        )
        candidates = []
        for item in metadata.get("items", []):
            status = item.get("status") or {}
            if (
                status.get("privacyStatus") not in {None, "public"}
                or status.get("embeddable") is False
            ):
                continue
            snippet = item.get("snippet") or {}
            candidate = VideoCandidate(
                video_id=str(item["id"]),
                url=f"https://www.youtube.com/watch?v={item['id']}",
                title=html.unescape(str(snippet.get("title") or "")),
                channel_id=snippet.get("channelId"),
                channel_title=html.unescape(str(snippet.get("channelTitle") or "")) or None,
                published_at=_parse_datetime(snippet.get("publishedAt")),
                view_count=_safe_int((item.get("statistics") or {}).get("viewCount")),
                duration_seconds=parse_iso_duration(
                    str((item.get("contentDetails") or {}).get("duration") or "")
                ),
                description=html.unescape(str(snippet.get("description") or ""))[:2000],
            )
            candidate.rejection_reason = candidate_rejection_reason(candidate, title)
            candidates.append(candidate)
        return YouTubeSearchResult(query=query, candidates=candidates)

    def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise YouTubeTemporaryError(f"YouTube request failed: {exc}") from exc
        if response.is_success:
            return response.json()

        message, reasons = _error_details(response)
        detail = f"YouTube API returned {response.status_code}: {message}"
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
            raise YouTubeQuotaExceeded(detail)
        if response.status_code in {400, 401, 403}:
            raise YouTubeUnavailable(detail)
        if response.status_code == 429 or response.status_code >= 500:
            raise YouTubeTemporaryError(detail)
        raise YouTubeError(detail)


def parse_iso_duration(value: str) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
        r"(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return 0
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def _matches_game(candidate_title: str, game_title: str) -> bool:
    wanted = _normalize(game_title)
    if f" {wanted} " in f" {candidate_title} ":
        return True
    tokens = [token for token in wanted.split() if token not in _TITLE_STOPWORDS]
    if not tokens:
        tokens = wanted.split()
    present = sum(token in candidate_title.split() for token in tokens)
    return bool(tokens) and present / len(tokens) >= 0.8


def _normalize(value: str) -> str:
    folded = value.casefold().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _error_details(response: httpx.Response) -> tuple[str, set[str]]:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return response.text[:500] or "unknown error", set()
    reasons = {
        str(item.get("reason"))
        for item in error.get("errors", [])
        if isinstance(item, dict) and item.get("reason")
    }
    return str(error.get("message") or response.text[:500] or "unknown error"), reasons

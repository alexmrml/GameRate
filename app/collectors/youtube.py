"""Read-only let's-play discovery and deterministic candidate filtering.

The search is served by yt-dlp and the metadata by the Data API's `videos.list`, because
each endpoint is the only one that can do its half. `search.list` has a separate daily
allowance of roughly 100 calls — small enough that it, not the model, was the ceiling on how
many games a day could be discovered — while `videos.list` costs 1 of 10 000 units and is
the only source of the category, full description and privacy fields the filter needs.
yt-dlp's flat search returns neither the category nor an untruncated description, so it
cannot replace the hydration step, and does not need to.
"""

import html
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx

from app.config import settings
from app.youtube_proxies import redact_proxy_from_message

YOUTUBE_BASE_URL = "https://www.googleapis.com/youtube/v3"
# YouTube's "sort by view count" search filter. The requirement is the most popular video
# among the suitable ones, and a relevance-ordered page does not contain it: ordered by
# relevance the pick for Creepshow fell from 24 743 views to 922, and for Mortal Shell II
# from 325k to 193k. Sorting also makes a 50-result page worth asking for.
VIEW_COUNT_SORT = "CAM%253D"
# videos.list takes at most 50 ids per call and costs one unit either way.
HYDRATION_BATCH = 50


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
    category_id: str | None = None
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
            category_id=data.get("category_id"),
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


# YouTube's search silently loses recall as a quoted OR-chain grows. Measured against one
# game on one day: 1 branch returned 22-25 results, 3 returned 25, 4 returned 7, and the 7
# branches this module used to send returned 1. Three is the most that stays safe, which is
# why the term list below is short and deliberately spans languages rather than synonyms.
MAX_QUERY_BRANCHES = 3
SEARCH_INTENT_TERMS = ("gameplay", "playthrough", "прохождение")

# A let's-play is long. Trailers, teasers, reveals and clipped highlights are not, so one
# duration bound removes most of them without a single keyword, and what survives it is
# already the right *shape* of video.
MIN_DURATION_SECONDS = 480

# Rejected outright: none of these is one creator playing this game and talking about it.
# Kept narrow on purpose — a term here costs recall on every small channel that happens to
# use the word, and the analysis stage rejects an opinion-free fragment on its own anyway.
_REJECT_TERMS = (
    # publisher and press material
    "trailer",
    "teaser",
    "announcement",
    "reveal",
    "gameplay demo",
    "gameplay showcase",
    "gameplay reveal",
    "official gameplay",
    "opening movie",
    "promotion movie",
    # opinion pieces that are not a playthrough
    "review",
    "before you buy",
    "first impressions",
    "retrospective",
    "video essay",
    "explained",
    "lore",
    "iceberg",
    "reaction",
    # instructional
    "guide",
    "tutorial",
    "how to",
    "tips and tricks",
    "beginners",
    "beginner",
    "tier list",
    "best build",
    "build guide",
    "all collectibles",
    "achievement",
    "trophy",
    "platinum",
    "speedrun",
    "world record",
    "glitch",
    "exploit",
    "farm",
    "farming",
    # assembled from other footage
    "compilation",
    "montage",
    "funny moments",
    "best moments",
    "highlights",
    "all bosses",
    "all deaths",
    "all cutscenes",
    "cutscene",
    "game movie",
    "full movie",
    "cinematic",
    "soundtrack",
    " ost ",
    "music video",
    "animation",
    "animated",
    "parody",
    # not this build of this game
    "mod",
    "mods",
    "modded",
    "benchmark",
    "fps test",
    "performance test",
    "graphics comparison",
    # Russian equivalents
    "обзор",
    "гайд",
    "трейлер",
    "саундтрек",
    "катсцены",
    "первый взгляд",
    "нарезка",
    "приколы",
)

# Speech is the entire input to the analysis, so a video that advertises its absence is
# worthless to us however good a let's-play it otherwise is.
_NO_COMMENTARY_TERMS = (
    "no commentary",
    "without commentary",
    "no commentary gameplay",
    "no talking",
    "silent playthrough",
    "без комментариев",
    "без комментария",
)

_SHORTS_TERMS = ("shorts", "short", "ytshorts", "reels")

# YouTube's own "Gaming" category. It costs nothing — the value rides along in the snippet
# already fetched — and it is what separates a game called Gallipoli or Superposition from
# the battle documentary and the circuit-theory lecture that share the word.
GAMING_CATEGORY_ID = "20"

# A game whose name is one or two ordinary words is not identified by that name alone, so
# for those, and only those, the video must also declare itself a playthrough somewhere.
# Applying this to every game is what used to reject small channels whose titles say only
# the game's name.
AMBIGUOUS_NAME_MAX_TOKENS = 2
# Creators name the game early and at the head of a segment: "Slayblade - Part 1",
# "ХОРРОР ► Creepshow", "This new horror game is insane! | FEED IT". A one-word name that
# turns up late or mid-sentence is usually another game's chapter or an ordinary verb —
# "JUSANT - Chapter 1 - Daymark", "...If I Feed It". Ambiguous names must clear both bars;
# distinctive names need no such help.
AMBIGUOUS_NAME_MAX_SEGMENTS = 2
_SEGMENT_SPLIT = re.compile(r"[|\-–—:;/#!?,.()\[\]►»▶★☆]+")
# "JUSANT - Chapter 1 - Daymark" is a chapter of Jusant, not a game called Daymark. When
# the run-up to an ambiguous name is a chapter or level counter, the name is a level label.
_LEVEL_LABEL = re.compile(
    r"\b(?:chapter|episode|level|act|stage|mission|part|глава|уровень|акт|часть)\s*\d+\s*$",
    re.IGNORECASE,
)
_INTENT_TERMS = (
    "gameplay",
    "playthrough",
    "lets play",
    "let s play",
    "walkthrough",
    "full game",
    "part 1",
    "episode 1",
    "ep 1",
    "livestream",
    "live stream",
    "stream",
    "first look",
    "playing",
    "played",
    "прохожд",
    "летспле",
    "геймпле",
    "играем",
    "играет",
    "стрим",
)

# Packaging that belongs to the store listing rather than to how anyone names a video.
_EDITION_SUFFIX = re.compile(
    r"\s*[-–—:]?\s*(?:the\s+)?"
    r"(?:legacy|definitive|deluxe|complete|goty|game\s+of\s+the\s+year|special|gold|"
    r"ultimate|anniversary|legendary|enhanced|extended)\s+edition\s*$",
    re.IGNORECASE,
)
_PARENTHETICAL = re.compile(r"[(\[][^)\]]*[)\]]")
_DOTTED_ACRONYM = re.compile(r"\b(?:[A-Za-z]\.){2,}")
_ROMAN_NUMERALS = {
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "xi": "11",
    "xii": "12",
}
_SEQUEL_MARKERS = set(_ROMAN_NUMERALS) | {str(number) for number in range(2, 13)}


def search_title(title: str) -> str:
    """The game's name as a person would type it: no bracketed suffix, no edition label."""
    cleaned = _PARENTHETICAL.sub(" ", title).replace('"', " ")
    cleaned = _EDITION_SUFFIX.sub("", " ".join(cleaned.split()))
    return " ".join(cleaned.split()) or " ".join(title.split())


def build_search_query(title: str) -> str:
    """One quoted-phrase query, kept to `MAX_QUERY_BRANCHES` so recall does not collapse."""
    name = search_title(title)
    branches = [f'"{name}" {term}' for term in SEARCH_INTENT_TERMS[:MAX_QUERY_BRANCHES]]
    return "|".join(branches)


def title_variants(title: str) -> list[str]:
    """Normalized phrases, any one of which names this game inside a video title.

    Every variant is matched as a contiguous phrase. An earlier token-coverage rule counted
    a title as a match when enough of its words appeared anywhere, which let "Dragon's Dogma
    2 ... chef-d'oeuvre" satisfy the game "Chef's Dogma".
    """
    variants: list[str] = []

    def add(value: str) -> None:
        normalized = _normalize(value)
        if normalized and normalized not in variants:
            variants.append(normalized)

    cleaned = search_title(title)
    add(cleaned)
    add(title)
    compact = _DOTTED_ACRONYM.sub(lambda match: match.group(0).replace(".", ""), cleaned)
    add(compact)
    for base in (cleaned, compact):
        add(_arabic_numerals(base))
    return variants


def candidate_rejection_reason(candidate: VideoCandidate, game_title: str) -> str | None:
    """Reject what is certainly not a let's-play of this game, and nothing else.

    Eligibility is deliberately a yes/no test rather than a score: the caller picks the most
    viewed survivor, so anything resembling a quality ranking belongs here as a hard rule or
    not at all.
    """
    normalized_title = _normalize(candidate.title)
    title_text = f" {normalized_title} "
    description_text = f" {_normalize(candidate.description[:1000])} "

    if any(f" {term} " in title_text for term in _SHORTS_TERMS):
        return "shorts"
    if candidate.duration_seconds < MIN_DURATION_SECONDS:
        return "too_short"
    if candidate.category_id is not None and candidate.category_id != GAMING_CATEGORY_ID:
        return "not_gaming"
    if not _matches_game(normalized_title, game_title):
        return "different_game"
    for term in _REJECT_TERMS:
        if f" {_normalize(term)} " in title_text:
            return f"excluded:{term.strip().replace(' ', '_')}"
    for term in _NO_COMMENTARY_TERMS:
        normalized_term = f" {_normalize(term)} "
        if normalized_term in title_text or normalized_term in description_text:
            return "no_commentary"
    if _name_is_ambiguous(game_title):
        if not _name_heads_a_segment(candidate.title, game_title):
            return "name_not_prominent"
        if not any(term in title_text or term in description_text for term in _INTENT_TERMS):
            return "no_gameplay_signal"
        # Someone playing this game says so in the description too. Where the name is a
        # level in a bigger game, the description is about that game instead.
        if not any(f" {name} " in description_text for name in title_variants(game_title)):
            return "name_absent_from_description"
    return None


def _name_is_ambiguous(game_title: str) -> bool:
    return len(_normalize(search_title(game_title)).split()) <= AMBIGUOUS_NAME_MAX_TOKENS


def _name_heads_a_segment(candidate_title: str, game_title: str) -> bool:
    variants = title_variants(game_title)
    segments = [
        normalized
        for segment in _SEGMENT_SPLIT.split(candidate_title)
        if (normalized := _normalize(segment))
    ]
    for index, normalized in enumerate(segments[:AMBIGUOUS_NAME_MAX_SEGMENTS]):
        if not any(normalized == name or normalized.startswith(f"{name} ") for name in variants):
            continue
        if index and _is_level_labelled(segments, index):
            continue
        return True
    return False


def _is_level_labelled(segments: list[str], index: int) -> bool:
    """Is the name at `index` a chapter of something else rather than the game itself?

    A counter on either side says so — "JUSANT - Chapter 1 - Daymark" and "Jusant - DAYMARK
    - Chapter 1" are the same video named two ways. Only a name that does *not* lead the
    title is judged this way: in "Slayblade - Part 1" the counter is this game's episode
    number, which is exactly what a let's-play looks like.
    """
    neighbours = [segments[index - 1]]
    if index + 1 < len(segments):
        neighbours.append(segments[index + 1])
    return any(_LEVEL_LABEL.search(segment) for segment in neighbours)


class YouTubeClient:
    """yt-dlp for the result list, one batched videos.list per 50 for their metadata."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        search_ids: Callable[[str, int, str | None], list[str]] | None = None,
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
        self._search_ids = search_ids or search_video_ids

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search_game(self, title: str, *, proxy: str | None = None) -> YouTubeSearchResult:
        """Search once, then hydrate every result before judging any of it."""
        query = build_search_query(title)
        video_ids = self._search_ids(query, settings.youtube_search_max_results, proxy)
        if not video_ids:
            return YouTubeSearchResult(query=query, candidates=[])

        candidates: list[VideoCandidate] = []
        for start in range(0, len(video_ids), HYDRATION_BATCH):
            batch = video_ids[start : start + HYDRATION_BATCH]
            metadata = self._get(
                "/videos",
                params={
                    "part": "snippet,contentDetails,statistics,status",
                    "id": ",".join(batch),
                    "maxResults": HYDRATION_BATCH,
                    "key": self.api_key,
                },
            )
            candidates.extend(self._candidates(metadata, title))
        return YouTubeSearchResult(query=query, candidates=candidates)

    def _candidates(self, metadata: dict[str, Any], title: str) -> list[VideoCandidate]:
        candidates = []
        for item in metadata.get("items", []):
            status = item.get("status") or {}
            if (
                status.get("privacyStatus") not in {None, "public"}
                or status.get("embeddable") is False
            ):
                continue
            snippet = item.get("snippet") or {}
            video_id = str(item["id"])
            candidate = VideoCandidate(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=html.unescape(str(snippet.get("title") or "")),
                channel_id=snippet.get("channelId"),
                channel_title=html.unescape(str(snippet.get("channelTitle") or "")) or None,
                published_at=_parse_datetime(snippet.get("publishedAt")),
                view_count=_safe_int((item.get("statistics") or {}).get("viewCount")),
                duration_seconds=parse_iso_duration(
                    str((item.get("contentDetails") or {}).get("duration") or "")
                ),
                description=html.unescape(str(snippet.get("description") or ""))[:2000],
                category_id=str(snippet.get("categoryId")) if snippet.get("categoryId") else None,
            )
            candidate.rejection_reason = candidate_rejection_reason(candidate, title)
            candidates.append(candidate)
        return candidates

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


def search_video_ids(query: str, limit: int, proxy: str | None = None) -> list[str]:
    """Run one view-count-ordered YouTube search through yt-dlp and return its video ids.

    `extract_flat` keeps this to the search page itself: no per-video extraction, no media,
    no cookies and no account. Measured from the container at about two seconds a search.
    """
    # Imported here so the web process never pays for yt-dlp's import cost.
    import yt_dlp
    from yt_dlp.utils import DownloadError, ExtractorError

    url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(query)
        + f"&sp={VIEW_COUNT_SORT}"
    )
    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "socket_timeout": settings.crawl_request_timeout_seconds,
    }
    if proxy:
        options["proxy"] = proxy
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        message = redact_proxy_from_message(str(exc), proxy)
        raise YouTubeTemporaryError(f"yt-dlp search failed: {message}") from exc
    except Exception as exc:  # an extractor surprise must not reach the crawler
        message = redact_proxy_from_message(str(exc), proxy)
        raise YouTubeTemporaryError(f"yt-dlp search crashed: {message}") from exc
    entries = (info or {}).get("entries") or []
    return list(
        dict.fromkeys(
            str(entry["id"]) for entry in entries if isinstance(entry, dict) and entry.get("id")
        )
    )


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
    """True when the video title names this game as a contiguous phrase.

    A trailing sequel marker is checked too: without it the game "Mortal Shell" would claim
    every "Mortal Shell II" video, because its name is a prefix of its sequel's.
    """
    words = candidate_title.split()
    for variant in title_variants(game_title):
        tokens = variant.split()
        if not tokens:
            continue
        span = len(tokens)
        for index in range(len(words) - span + 1):
            if words[index : index + span] != tokens:
                continue
            following = words[index + span] if index + span < len(words) else ""
            if following in _SEQUEL_MARKERS and tokens[-1] not in _SEQUEL_MARKERS:
                continue  # this is the sequel, not the game asked about
            return True
    return False


def _arabic_numerals(value: str) -> str:
    return " ".join(_ROMAN_NUMERALS.get(word.casefold(), word) for word in value.split())


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

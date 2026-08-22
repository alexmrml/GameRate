"""YouTube caption reader built on yt-dlp: subtitles without any media download.

Like the other collectors this module never touches the database. It answers two
questions and nothing else: which caption track carries what the creator actually said,
and which stretch of the video is worth analysing.

The second question is why this module exists rather than a two-line yt-dlp call. A
let's-play ends with menus, credits, outro music or an idle camera far more often than
it ends with commentary, so a fixed "last N minutes" fragment regularly contains no
speech at all. :func:`select_tail_window` therefore scans backwards from the end and
takes the *latest* window that still carries a real speech rate.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.youtube_proxies import redact_proxy_from_message

logger = logging.getLogger("gamerate.collectors.transcript")

CAPTION_FORMAT = "json3"
# How far back from the end a window may be taken, as a multiple of the window itself.
# Three windows keep "near the end" meaningful for a nine-hour stream while still
# allowing a short video to be scanned completely.
SEARCH_SPAN_FACTOR = 3
MAX_WINDOW_CHARACTERS = 24_000

_BRACKETED = re.compile(r"^[\[(][^\])]*[\])]$")
_UNAVAILABLE_MARKERS = (
    "private video",
    "video unavailable",
    "video is unavailable",
    "has been removed",
    "no longer available",
    "not available in your country",
    "age-restricted",
    "age restricted",
    "sign in to confirm",
    "members-only",
    "this live event",
)


class TranscriptError(RuntimeError):
    """Base class for caption retrieval failures."""


class TranscriptUnavailable(TranscriptError):
    """The video exists but publishes no usable caption track."""


class TranscriptVideoUnavailable(TranscriptError):
    """The video itself cannot be read: private, removed, gated or region-locked."""


class TranscriptTemporaryError(TranscriptError):
    """A transient extractor or transport failure; the same video may work later."""


class TranscriptTooQuiet(TranscriptError):
    """Captions exist, but no part of the video carries enough speech to analyse."""


@dataclass(slots=True)
class Cue:
    start: float
    end: float
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass(slots=True)
class TranscriptTrack:
    video_id: str
    language: str
    is_automatic: bool
    duration_seconds: float
    cues: list[Cue]

    @property
    def word_count(self) -> int:
        return sum(cue.word_count for cue in self.cues)


@dataclass(slots=True)
class TranscriptWindow:
    start_seconds: int
    end_seconds: int
    text: str
    words_per_minute: float
    language: str
    is_automatic: bool

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def parse_json3(payload: str | bytes) -> list[Cue]:
    """Turn YouTube's `json3` timed text into plain cues.

    Automatic captions repeat the previous line in `aAppend` events so the on-screen
    text can scroll; keeping them would double every word and distort the speech rate.
    Bracketed sound labels such as `[Music]` are not speech and are dropped as well.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise TranscriptUnavailable(f"Caption payload was not valid JSON: {exc}") from exc

    cues: list[Cue] = []
    for event in data.get("events") or []:
        if not isinstance(event, dict) or event.get("aAppend"):
            continue
        raw = "".join(
            str(segment.get("utf8") or "")
            for segment in event.get("segs") or []
            if isinstance(segment, dict)
        )
        text = " ".join(raw.split())
        if not text or _BRACKETED.match(text):
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        duration = float(event.get("dDurationMs") or 0) / 1000.0
        cues.append(Cue(start=start, end=start + duration, text=text))
    cues.sort(key=lambda cue: cue.start)
    return cues


def choose_caption_track(info: dict[str, Any]) -> tuple[str, str, bool]:
    """Pick the track that carries the creator's own words.

    YouTube offers machine translations of the automatic captions under every language
    code it supports, and those URLs carry `tlang=`. A translation of a transcription is
    the worst possible evidence for a verbatim quote, so only original tracks are
    considered and the video's own language wins over everything else.
    """
    for store, is_automatic in (
        (info.get("subtitles"), False),
        (info.get("automatic_captions"), True),
    ):
        available = _original_tracks(store)
        if not available:
            continue
        language = _preferred_language(available, info.get("language"))
        return available[language], language, is_automatic
    raise TranscriptUnavailable("The video publishes no subtitles or automatic captions")


def select_tail_window(
    track: TranscriptTrack,
    *,
    window_seconds: int,
    min_words_per_minute: float,
) -> TranscriptWindow:
    """Return the latest window near the end that still carries real commentary.

    The bar is an anomaly filter, not a ranking: it is set far below an ordinary speech
    rate, so the scan stops at the first window that clears it and a video whose creator
    talks to the last second is analysed at its very end. Only silence — credits, menus,
    outro music — pushes the window earlier. When nothing in the searched tail clears the
    bar, the densest window in that tail is used, provided it holds at least half the
    required rate; below that the source carries no commentary worth sending to a model.
    """
    duration = track.duration_seconds or (track.cues[-1].end if track.cues else 0.0)
    if not track.cues or duration <= 0:
        raise TranscriptTooQuiet("The caption track contains no speech")

    window = min(float(window_seconds), duration)
    earliest_start = max(0.0, duration - window * SEARCH_SPAN_FACTOR)
    step = max(60.0, window / 5)

    best: TranscriptWindow | None = None
    start = duration - window
    while True:
        current = _window_at(track, start, window)
        if current.words_per_minute >= min_words_per_minute:
            return current
        if best is None or current.words_per_minute > best.words_per_minute:
            best = current
        if start <= earliest_start:
            break
        start = max(earliest_start, start - step)

    if best is not None and best.words_per_minute >= min_words_per_minute / 2:
        return best
    rate = best.words_per_minute if best is not None else 0.0
    raise TranscriptTooQuiet(f"The busiest window holds only {rate:.0f} words per minute")


class TranscriptClient:
    """yt-dlp metadata plus a yt-dlp network fetch of the caption track.

    `download=False` together with `skip_download` keeps this to the player response and
    the timed-text endpoint: no video stream, no audio stream, no temporary files.
    """

    def __init__(
        self,
        *,
        extract_info: Callable[[str, str | None], dict[str, Any]] | None = None,
        fetch_url: Callable[[str, str | None], bytes] | None = None,
        timeout: float | None = None,
    ) -> None:
        self._timeout = (
            timeout if timeout is not None else settings.youtube_transcript_timeout_seconds
        )
        self._extract_info = extract_info or self._default_extract_info
        self._fetch_url = fetch_url or self._default_fetch_url

    def close(self) -> None:  # symmetry with the other collector clients
        return None

    def fetch(self, video_id: str, *, proxy: str | None = None) -> TranscriptTrack:
        info = self._extract_info(f"https://www.youtube.com/watch?v={video_id}", proxy)
        url, language, is_automatic = choose_caption_track(info)
        cues = parse_json3(self._fetch_url(url, proxy))
        if not cues:
            raise TranscriptUnavailable("The caption track decoded to no speech cues")
        return TranscriptTrack(
            video_id=video_id,
            language=language,
            is_automatic=is_automatic,
            duration_seconds=float(info.get("duration") or 0.0),
            cues=cues,
        )

    def _default_extract_info(self, url: str, proxy: str | None) -> dict[str, Any]:
        # Imported here so the web process never pays for yt-dlp's import cost.
        import yt_dlp
        from yt_dlp.utils import DownloadError, ExtractorError

        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": self._timeout,
        }
        if proxy:
            options["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
        except (DownloadError, ExtractorError) as exc:
            message = redact_proxy_from_message(str(exc), proxy)
            if any(marker in message.casefold() for marker in _UNAVAILABLE_MARKERS):
                raise TranscriptVideoUnavailable(f"yt-dlp could not read {url}: {message}") from exc
            raise TranscriptTemporaryError(f"yt-dlp failed for {url}: {message}") from exc
        except Exception as exc:  # extractor surprises must not reach the crawler
            message = redact_proxy_from_message(str(exc), proxy)
            raise TranscriptTemporaryError(f"yt-dlp crashed for {url}: {message}") from exc
        if not isinstance(info, dict):
            raise TranscriptTemporaryError(f"yt-dlp returned no metadata for {url}")
        return info

    def _default_fetch_url(self, url: str, proxy: str | None) -> bytes:
        import yt_dlp

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": self._timeout,
        }
        if proxy:
            options["proxy"] = proxy
        try:
            with (
                yt_dlp.YoutubeDL(options) as downloader,
                downloader.urlopen(url) as response,
            ):
                status_code = getattr(response, "status", None) or response.getcode()
                payload = response.read()
        except Exception as exc:
            message = redact_proxy_from_message(str(exc), proxy)
            status_code = getattr(exc, "status", None) or getattr(exc, "code", None)
            if status_code == 404:
                raise TranscriptUnavailable(
                    "The caption track URL has expired or was withdrawn"
                ) from exc
            raise TranscriptTemporaryError(f"Caption download failed: {message}") from exc
        if status_code == 404:
            raise TranscriptUnavailable("The caption track URL has expired or was withdrawn")
        if status_code is not None and not 200 <= status_code < 300:
            raise TranscriptTemporaryError(f"Caption download returned {status_code}")
        return payload


def _original_tracks(store: Any) -> dict[str, str]:
    tracks: dict[str, str] = {}
    if not isinstance(store, dict):
        return tracks
    for language, entries in store.items():
        if language == "live_chat" or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("ext") != CAPTION_FORMAT:
                continue
            url = str(entry.get("url") or "")
            if url and "tlang=" not in url:
                tracks[str(language)] = url
                break
    return tracks


def _preferred_language(available: dict[str, str], video_language: Any) -> str:
    wanted = str(video_language or "").strip()
    for code in (wanted, wanted.split("-")[0]):
        if code and code in available:
            return code
    for code in sorted(available):
        if code == "en" or code.startswith("en-"):
            return code
    return sorted(available)[0]


def _window_at(track: TranscriptTrack, start: float, window: float) -> TranscriptWindow:
    start = max(0.0, start)
    end = start + window
    texts = [cue.text for cue in track.cues if start <= cue.start < end]
    words = sum(len(text.split()) for text in texts)
    text = " ".join(texts)
    if len(text) > MAX_WINDOW_CHARACTERS:
        text = text[:MAX_WINDOW_CHARACTERS].rsplit(" ", 1)[0]
    return TranscriptWindow(
        start_seconds=int(start),
        end_seconds=int(end),
        text=text,
        words_per_minute=words / (window / 60) if window > 0 else 0.0,
        language=track.language,
        is_automatic=track.is_automatic,
    )

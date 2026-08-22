"""Best-effort YouTube discovery and speech-grounded let's-play analysis.

The main path never touches media: yt-dlp reads the video's own subtitles, a density
scan picks the stretch near the end that still carries commentary, and that text is
analysed like any other text. Only a source that publishes no usable subtitles falls
back to sending the video itself to a multimodal model, which is why that fallback has
its own small per-run budget while the main path does not.

One row per game is both the durable state machine and the search cache. A successful
analysis is stable; provider failures wait before retrying, and a source with no useful
commentary advances to the next cached candidate without another search.list call.
"""

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.gemini import (
    TRANSCRIPT_SOURCE,
    VIDEO_SOURCE,
    YOUTUBE_PROMPT_VERSION,
    GeminiClient,
    GeminiInvalidResponse,
    GeminiTemporaryError,
    GeminiUnavailable,
    GeminiVideoUnavailable,
    YouTubeVideoResult,
)
from app.collectors.transcript import (
    TranscriptClient,
    TranscriptError,
    TranscriptTemporaryError,
    TranscriptTooQuiet,
    TranscriptUnavailable,
    TranscriptVideoUnavailable,
    TranscriptWindow,
    select_tail_window,
)
from app.collectors.youtube import (
    VideoCandidate,
    YouTubeClient,
    YouTubeError,
    YouTubeQuotaExceeded,
    YouTubeSearchResult,
    YouTubeTemporaryError,
    YouTubeUnavailable,
)
from app.config import settings
from app.models import Game, YouTubeAnalysis
from app.services.app_settings import effective_youtube_proxies, get_setting
from app.time import as_utc, utc_now

logger = logging.getLogger("gamerate.youtube")

PENDING = "pending"
SUCCESS = "success"
NO_CANDIDATE = "no_candidate"
YOUTUBE_ERROR = "youtube_error"
YOUTUBE_UNAVAILABLE = "youtube_unavailable"
YOUTUBE_QUOTA_EXHAUSTED = "youtube_quota_exhausted"
VIDEO_UNAVAILABLE = "video_unavailable"
NO_TRANSCRIPT = "no_transcript"
TRANSCRIPT_ERROR = "transcript_error"
GEMINI_FAILED = "gemini_failed"
GEMINI_UNAVAILABLE = "gemini_unavailable"
GEMINI_QUOTA_EXHAUSTED = "gemini_quota_exhausted"
NO_USEFUL_COMMENTARY = "no_useful_commentary"
UNCHANGED = "unchanged"

_RETRY_SAME_SOURCE = {
    PENDING,
    TRANSCRIPT_ERROR,
    GEMINI_FAILED,
    GEMINI_UNAVAILABLE,
    GEMINI_QUOTA_EXHAUSTED,
}
_TRY_NEXT_SOURCE = {VIDEO_UNAVAILABLE, NO_USEFUL_COMMENTARY, NO_TRANSCRIPT}

# Reading subtitles is cheap and makes no model call, so a game may walk a few cached
# candidates in one pass looking for one that actually publishes captions.
MAX_TRANSCRIPT_ATTEMPTS = 3

# yt-dlp breaking on a video says nothing about whether Gemini can watch it, so a
# technical caption failure must not lock a game out of the fallback for good. It is still
# worth one plain retry first: a single timeout is far more likely to be a blip than a
# video that will never yield captions, and the fallback's budget is the scarce one.
TRANSCRIPT_ERRORS_BEFORE_FALLBACK = 2


@dataclass(slots=True)
class YouTubeOutcome:
    title: str
    status: str = UNCHANGED
    video_id: str | None = None
    source: str | None = None
    search_called: bool = False
    transcript_reads: int = 0
    gemini_called: bool = False
    error: str | None = None

    def as_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {"title": self.title, "status": self.status}
        if self.video_id:
            details["video_id"] = self.video_id
        if self.source:
            details["source"] = self.source
        if self.search_called:
            details["search_called"] = True
        if self.transcript_reads:
            details["transcript_reads"] = self.transcript_reads
        if self.gemini_called:
            details["gemini_called"] = True
        if self.error:
            details["error"] = self.error
        return details


def youtube_needs_work(row: YouTubeAnalysis | None) -> bool:
    if row is None:
        return True
    if row.status == SUCCESS:
        version = (row.analysis_data or {}).get("prompt_version")
        return version != YOUTUBE_PROMPT_VERSION
    if row.next_retry_at is None:
        return True
    return as_utc(row.next_retry_at) <= utc_now()


class YouTubeEnrichmentSession:
    """Provider clients, per-run budgets and failure isolation for one processing run."""

    def __init__(
        self,
        db: Session,
        *,
        youtube_client: YouTubeClient | Any | None = None,
        transcript_client: TranscriptClient | Any | None = None,
        gemini_client: GeminiClient | Any | None = None,
        video_gemini_client: GeminiClient | Any | None = None,
        proxies: list[str] | None = None,
        proxy_selector: Callable[[list[str]], str] | None = None,
    ) -> None:
        self.enabled = bool(get_setting(db, "youtube.enabled"))
        self.model = str(get_setting(db, "youtube.model"))
        self.video_fallback_model = str(get_setting(db, "youtube.video_fallback_model"))
        self.fragment_minutes = int(get_setting(db, "youtube.fragment_minutes"))
        self.min_words_per_minute = int(get_setting(db, "youtube.min_words_per_minute"))
        self.max_games = int(get_setting(db, "youtube.max_games_per_run"))
        self.max_video_fallbacks = int(get_setting(db, "youtube.max_video_fallbacks_per_run"))
        self.disabled_reason: str | None = None
        self.youtube_disabled_reason: str | None = None
        self.gemini_disabled_reason: str | None = None
        self.video_disabled_reason: str | None = None
        self.search_calls = 0
        self.transcript_reads = 0
        self.gemini_calls = 0
        self.video_fallback_calls = 0
        self._youtube = youtube_client
        self._transcript = transcript_client
        self._gemini = gemini_client
        self._video_gemini = video_gemini_client
        self.proxies = effective_youtube_proxies(db) if proxies is None else list(proxies)
        self._proxy_selector = proxy_selector or secrets.choice

        missing = []
        if youtube_client is None and not settings.google_cloud_api_key:
            missing.append("GOOGLE_CLOUD_API_KEY")
        if gemini_client is None and not settings.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        if not self.enabled:
            self.disabled_reason = "YouTube analysis is turned off in settings"
        elif missing:
            self.enabled = False
            self.disabled_reason = f"{', '.join(missing)} is not configured"

    def close(self) -> None:
        for client in (self._youtube, self._transcript):
            if client is not None and hasattr(client, "close"):
                client.close()

    def youtube_client(self) -> YouTubeClient:
        if self._youtube is None:
            self._youtube = YouTubeClient()
        return self._youtube

    def transcript_client(self) -> TranscriptClient:
        if self._transcript is None:
            self._transcript = TranscriptClient()
        return self._transcript

    def gemini_client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient(model=self.model)
        return self._gemini

    def video_gemini_client(self) -> GeminiClient:
        if self._video_gemini is None:
            self._video_gemini = GeminiClient(model=self.video_fallback_model)
        return self._video_gemini

    # --- one game -----------------------------------------------------------------

    def enrich_game(self, db: Session, game: Game) -> YouTubeOutcome:
        """Produce at most one summary for a game, from at most one video."""
        outcome = YouTubeOutcome(title=game.title)
        if not self.enabled or self.disabled_reason:
            outcome.error = self.disabled_reason
            return outcome

        row = db.scalar(select(YouTubeAnalysis).where(YouTubeAnalysis.game_id == game.id))
        if not youtube_needs_work(row):
            outcome.video_id = row.video_id if row else None
            return outcome
        # Keep one egress identity for the entire game: discovery, candidate retries and
        # caption download all use the same randomly selected proxy.
        proxy = self._proxy_selector(self.proxies) if self.proxies else None
        if row is None:
            now = utc_now()
            row = YouTubeAnalysis(game_id=game.id, status=PENDING, created_at=now, updated_at=now)
            db.add(row)
            db.flush()

        candidate = self._source_for_attempt(row)
        if candidate is None:
            candidate = self._discover(row, game, outcome, proxy=proxy)
            if candidate is None:
                outcome.status = row.status
                db.flush()
                return outcome

        window, fallback_source = self._find_transcript(db, row, candidate, outcome, proxy=proxy)
        if window is not None:
            self._analyze_transcript(row, game, window, outcome)
        elif fallback_source is not None:
            self._analyze_video(row, game, fallback_source, outcome)
        else:
            outcome.status = row.status
            outcome.error = outcome.error or row.status_reason
        db.flush()
        return outcome

    def _find_transcript(
        self,
        db: Session,
        row: YouTubeAnalysis,
        candidate: VideoCandidate,
        outcome: YouTubeOutcome,
        *,
        proxy: str | None,
    ) -> tuple[TranscriptWindow | None, VideoCandidate | None]:
        """Walk cached candidates until one publishes usable subtitles.

        Returns the chosen window and, separately, the candidate the video fallback may
        use — only ever a video whose *captions* were missing, never one the extractor
        could not read at all.
        """
        fallback_source: VideoCandidate | None = None
        client = self.transcript_client()

        for attempt in range(MAX_TRANSCRIPT_ATTEMPTS):
            self._apply_candidate(row, candidate)
            outcome.video_id = candidate.video_id
            self.transcript_reads += 1
            outcome.transcript_reads += 1
            try:
                track = client.fetch(candidate.video_id, proxy=proxy)
                window = select_tail_window(
                    track,
                    window_seconds=self.fragment_minutes * 60,
                    min_words_per_minute=self.min_words_per_minute,
                )
            except TranscriptVideoUnavailable as exc:
                outcome.error = str(exc)
                self._mark_attempted(row)
                self._failure(row, VIDEO_UNAVAILABLE, str(exc))
            except (TranscriptUnavailable, TranscriptTooQuiet) as exc:
                # The video plays fine, it simply has no usable captions: the one case
                # the multimodal fallback exists for.
                outcome.error = str(exc)
                fallback_source = fallback_source or candidate
                self._mark_attempted(row)
                self._failure(row, NO_TRANSCRIPT, str(exc))
            except (TranscriptTemporaryError, TranscriptError) as exc:
                self._failure(row, TRANSCRIPT_ERROR, str(exc))
                outcome.error = str(exc)
                return None, self._fallback_after_error(row, candidate)
            except Exception as exc:  # a yt-dlp surprise must not reach the crawler
                logger.exception("Transcript read crashed video=%s", candidate.video_id)
                self._failure(row, TRANSCRIPT_ERROR, f"{type(exc).__name__}: {exc}")
                outcome.error = row.status_reason
                return None, self._fallback_after_error(row, candidate)
            else:
                self._clear_transcript_errors(row)
                return window, None

            db.flush()
            if attempt + 1 >= MAX_TRANSCRIPT_ATTEMPTS:
                break
            following = _next_cached_candidate(row)
            if following is None:
                break
            candidate = following

        return None, fallback_source

    def _analyze_transcript(
        self,
        row: YouTubeAnalysis,
        game: Game,
        window: TranscriptWindow,
        outcome: YouTubeOutcome,
    ) -> None:
        row.fragment_start_seconds = window.start_seconds
        row.fragment_end_seconds = window.end_seconds
        row.transcript_language = window.language
        row.transcript_is_automatic = window.is_automatic
        self._reset_status(row)
        outcome.source = TRANSCRIPT_SOURCE

        if self.gemini_disabled_reason:
            self._failure(row, GEMINI_UNAVAILABLE, self.gemini_disabled_reason)
            outcome.status = row.status
            outcome.error = row.status_reason
            return
        try:
            client = self.gemini_client()
            outcome.gemini_called = True
            self.gemini_calls += 1
            result = client.analyze_letsplay_transcript(
                game_title=game.title,
                transcript=window.text,
                language=window.language,
                start_seconds=window.start_seconds,
                end_seconds=window.end_seconds,
            )
        except GeminiUnavailable as exc:
            self.gemini_disabled_reason = str(exc)
            self._failure(row, GEMINI_UNAVAILABLE, str(exc))
            outcome.error = str(exc)
        except GeminiTemporaryError as exc:
            status = GEMINI_QUOTA_EXHAUSTED if _is_quota_error(exc) else GEMINI_FAILED
            self._failure(row, status, str(exc))
            outcome.error = str(exc)
        except GeminiInvalidResponse as exc:
            self._failure(row, GEMINI_FAILED, str(exc))
            outcome.error = str(exc)
        except Exception as exc:  # a new SDK failure must remain isolated from the crawler
            logger.exception("Transcript analysis crashed game=%s", game.id)
            self._failure(row, GEMINI_FAILED, f"{type(exc).__name__}: {exc}")
            outcome.error = row.status_reason
        else:
            self._store_result(row, result, transcript=window.text, extra=_window_details(window))
        outcome.status = row.status

    def _analyze_video(
        self,
        row: YouTubeAnalysis,
        game: Game,
        candidate: VideoCandidate,
        outcome: YouTubeOutcome,
    ) -> None:
        """Send the video itself, only for a source that publishes no usable subtitles."""
        if self.video_fallback_calls >= max(self.max_video_fallbacks, 0):
            outcome.status = row.status
            outcome.error = row.status_reason
            return
        if self.video_disabled_reason:
            self._failure(row, GEMINI_UNAVAILABLE, self.video_disabled_reason)
            outcome.status = row.status
            outcome.error = row.status_reason
            return

        self._apply_candidate(row, candidate)
        outcome.video_id = candidate.video_id
        outcome.source = VIDEO_SOURCE
        end_seconds = max(candidate.duration_seconds, 0)
        start_seconds = max(0, end_seconds - self.fragment_minutes * 60)
        row.fragment_start_seconds = start_seconds
        row.fragment_end_seconds = end_seconds
        row.transcript_language = None
        row.transcript_is_automatic = None
        self._reset_status(row)

        try:
            client = self.video_gemini_client()
            outcome.gemini_called = True
            self.video_fallback_calls += 1
            result = client.analyze_youtube_video(
                game_title=game.title,
                video_url=candidate.url,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
        except GeminiVideoUnavailable as exc:
            self._mark_attempted(row)
            self._failure(row, VIDEO_UNAVAILABLE, str(exc))
            outcome.error = str(exc)
        except GeminiUnavailable as exc:
            self.video_disabled_reason = str(exc)
            self._failure(row, GEMINI_UNAVAILABLE, str(exc))
            outcome.error = str(exc)
        except GeminiTemporaryError as exc:
            status = GEMINI_QUOTA_EXHAUSTED if _is_quota_error(exc) else GEMINI_FAILED
            self._failure(row, status, str(exc))
            outcome.error = str(exc)
        except GeminiInvalidResponse as exc:
            self._failure(row, GEMINI_FAILED, str(exc))
            outcome.error = str(exc)
        except Exception as exc:  # a new SDK failure must remain isolated from the crawler
            logger.exception("YouTube video fallback crashed game=%s", game.id)
            self._failure(row, GEMINI_FAILED, f"{type(exc).__name__}: {exc}")
            outcome.error = row.status_reason
        else:
            self._store_result(row, result, transcript=result.speech_transcript, extra={})
        outcome.status = row.status

    def _store_result(
        self,
        row: YouTubeAnalysis,
        result: YouTubeVideoResult,
        *,
        transcript: str,
        extra: dict[str, Any],
    ) -> None:
        """Persist a result, keeping only the findings their own quote actually supports."""
        now = utc_now()
        validated_liked = [
            statement
            for statement, evidence in zip(result.liked, result.liked_evidence, strict=False)
            if _evidence_is_in_transcript(evidence, transcript)
        ]
        validated_disliked = [
            statement
            for statement, evidence in zip(result.disliked, result.disliked_evidence, strict=False)
            if _evidence_is_in_transcript(evidence, transcript)
        ]
        row.speech_transcript = transcript
        row.summary = result.overall_impression
        row.liked = validated_liked
        row.disliked = validated_disliked
        row.analysis_source = result.source
        row.analysis_data = {
            "prompt_version": result.prompt_version,
            "source": result.source,
            "has_useful_commentary": result.has_useful_commentary,
            "overall_opinion_evidence": result.overall_opinion_evidence,
            "overall_impression": result.overall_impression,
            "liked": [
                {"statement": statement, "speech_evidence": evidence}
                for statement, evidence in zip(result.liked, result.liked_evidence, strict=False)
            ],
            "disliked": [
                {"statement": statement, "speech_evidence": evidence}
                for statement, evidence in zip(
                    result.disliked, result.disliked_evidence, strict=False
                )
            ],
            "validated_liked": validated_liked,
            "validated_disliked": validated_disliked,
            **extra,
        }
        row.model_name = result.model
        row.analyzed_at = now
        row.updated_at = now
        useful = (
            result.has_useful_commentary
            and bool(result.overall_impression)
            and _evidence_is_in_transcript(result.overall_opinion_evidence, transcript)
        )
        if useful:
            row.status = SUCCESS
            row.status_reason = None
            row.next_retry_at = None
        else:
            self._mark_attempted(row)
            row.status = NO_USEFUL_COMMENTARY
            row.status_reason = "The selected fragment contained no useful creator opinion"
            row.next_retry_at = now + timedelta(hours=settings.youtube_retry_interval_hours)

    # --- source selection ---------------------------------------------------------

    def _source_for_attempt(self, row: YouTubeAnalysis) -> VideoCandidate | None:
        if row.video_id and row.status in _RETRY_SAME_SOURCE:
            return _candidate_from_row(row)
        if row.status in _TRY_NEXT_SOURCE:
            self._mark_attempted(row)
            return _next_cached_candidate(row)
        return None

    def _discover(
        self,
        row: YouTubeAnalysis,
        game: Game,
        outcome: YouTubeOutcome,
        *,
        proxy: str | None,
    ) -> VideoCandidate | None:
        cached = _next_cached_candidate(row)
        if cached is not None:
            return cached
        if self.youtube_disabled_reason:
            self._failure(row, YOUTUBE_UNAVAILABLE, self.youtube_disabled_reason)
            outcome.status = row.status
            outcome.error = row.status_reason
            return None
        try:
            client = self.youtube_client()
            outcome.search_called = True
            self.search_calls += 1
            result = client.search_game(game.title, proxy=proxy)
        except YouTubeQuotaExceeded as exc:
            self.youtube_disabled_reason = str(exc)
            self._failure(row, YOUTUBE_QUOTA_EXHAUSTED, str(exc))
            outcome.error = str(exc)
            return None
        except YouTubeUnavailable as exc:
            self.youtube_disabled_reason = str(exc)
            self._failure(row, YOUTUBE_UNAVAILABLE, str(exc))
            outcome.error = str(exc)
            return None
        except (YouTubeTemporaryError, YouTubeError) as exc:
            self._failure(row, YOUTUBE_ERROR, str(exc))
            outcome.error = str(exc)
            return None
        except Exception as exc:
            logger.exception("YouTube discovery crashed game=%s", game.id)
            self._failure(row, YOUTUBE_ERROR, f"{type(exc).__name__}: {exc}")
            outcome.error = row.status_reason
            return None

        now = utc_now()
        attempted = list((row.search_data or {}).get("attempted_video_ids") or [])
        row.search_query = result.query
        row.search_attempted_at = now
        row.search_data = {
            "query": result.query,
            "candidates": [item.to_cache() for item in result.candidates],
            "attempted_video_ids": attempted,
        }
        selected = result.selected(set(attempted))
        if selected is None:
            row.status = NO_CANDIDATE
            row.status_reason = "No suitable let's-play candidate was found"
            row.next_retry_at = now + timedelta(days=settings.youtube_no_result_refresh_days)
            row.updated_at = now
            outcome.status = NO_CANDIDATE
            return None
        return selected

    def _apply_candidate(self, row: YouTubeAnalysis, candidate: VideoCandidate) -> None:
        changed = row.video_id != candidate.video_id
        row.video_id = candidate.video_id
        row.video_url = candidate.url
        row.video_title = candidate.title
        row.channel_id = candidate.channel_id
        row.channel_title = candidate.channel_title
        row.published_at = candidate.published_at
        row.view_count = candidate.view_count
        row.duration_seconds = candidate.duration_seconds
        if changed:
            self._clear_transcript_errors(row)
            row.speech_transcript = None
            row.summary = None
            row.liked = None
            row.disliked = None
            row.analysis_data = None
            row.analysis_source = None
            row.transcript_language = None
            row.transcript_is_automatic = None
            row.model_name = None
            row.analyzed_at = None

    def _reset_status(self, row: YouTubeAnalysis) -> None:
        row.status = PENDING
        row.status_reason = None
        row.next_retry_at = None
        row.updated_at = utc_now()

    def _failure(self, row: YouTubeAnalysis, status: str, reason: str) -> None:
        now = utc_now()
        row.status = status
        row.status_reason = reason[:1000]
        row.next_retry_at = now + timedelta(hours=settings.youtube_retry_interval_hours)
        row.updated_at = now

    def _fallback_after_error(
        self, row: YouTubeAnalysis, candidate: VideoCandidate
    ) -> VideoCandidate | None:
        """Offer the source to the video fallback once caption reads keep failing on it."""
        data = dict(row.search_data or {})
        streak = int(data.get("transcript_errors") or 0) + 1
        data["transcript_errors"] = streak
        row.search_data = data
        return candidate if streak >= TRANSCRIPT_ERRORS_BEFORE_FALLBACK else None

    def _clear_transcript_errors(self, row: YouTubeAnalysis) -> None:
        data = row.search_data or {}
        if data.get("transcript_errors"):
            row.search_data = {**data, "transcript_errors": 0}

    def _mark_attempted(self, row: YouTubeAnalysis) -> None:
        if not row.video_id:
            return
        data = dict(row.search_data or {})
        attempted = list(data.get("attempted_video_ids") or [])
        if row.video_id not in attempted:
            attempted.append(row.video_id)
        data["attempted_video_ids"] = attempted
        row.search_data = data


def _window_details(window: TranscriptWindow) -> dict[str, Any]:
    return {
        "transcript_language": window.language,
        "transcript_is_automatic": window.is_automatic,
        "transcript_words": window.word_count,
        "words_per_minute": round(window.words_per_minute, 1),
    }


def _next_cached_candidate(row: YouTubeAnalysis) -> VideoCandidate | None:
    data = row.search_data or {}
    attempted = set(data.get("attempted_video_ids") or [])
    candidates = [
        VideoCandidate.from_cache(item)
        for item in data.get("candidates", [])
        if isinstance(item, dict)
    ]
    return YouTubeSearchResult(query=str(data.get("query") or ""), candidates=candidates).selected(
        attempted
    )


def _candidate_from_row(row: YouTubeAnalysis) -> VideoCandidate:
    return VideoCandidate(
        video_id=str(row.video_id),
        url=row.video_url or f"https://www.youtube.com/watch?v={row.video_id}",
        title=row.video_title or str(row.video_id),
        channel_id=row.channel_id,
        channel_title=row.channel_title,
        published_at=row.published_at,
        view_count=row.view_count or 0,
        duration_seconds=row.duration_seconds or 0,
    )


def _is_quota_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(term in message for term in ("429", "quota", "resource_exhausted"))


def _evidence_is_in_transcript(evidence: str, transcript: str) -> bool:
    def normalized(value: str) -> str:
        return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())

    quote = normalized(evidence)
    return len(quote.split()) >= 3 and quote in normalized(transcript)

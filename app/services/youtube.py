"""Best-effort YouTube discovery and speech-grounded Gemini analysis.

One row per game is both the durable state machine and the search cache. A successful
analysis is stable; provider failures wait before retrying, and a source with no useful
commentary advances to the next cached candidate without another search.list call.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.gemini import (
    YOUTUBE_PROMPT_VERSION,
    GeminiClient,
    GeminiInvalidResponse,
    GeminiTemporaryError,
    GeminiUnavailable,
    GeminiVideoUnavailable,
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
from app.services.app_settings import get_setting
from app.time import as_utc, utc_now

logger = logging.getLogger("gamerate.youtube")

PENDING = "pending"
SUCCESS = "success"
NO_CANDIDATE = "no_candidate"
YOUTUBE_ERROR = "youtube_error"
YOUTUBE_UNAVAILABLE = "youtube_unavailable"
YOUTUBE_QUOTA_EXHAUSTED = "youtube_quota_exhausted"
VIDEO_UNAVAILABLE = "video_unavailable"
GEMINI_FAILED = "gemini_failed"
GEMINI_UNAVAILABLE = "gemini_unavailable"
GEMINI_QUOTA_EXHAUSTED = "gemini_quota_exhausted"
NO_USEFUL_COMMENTARY = "no_useful_commentary"
UNCHANGED = "unchanged"

_RETRY_SAME_SOURCE = {PENDING, GEMINI_FAILED, GEMINI_UNAVAILABLE, GEMINI_QUOTA_EXHAUSTED}
_TRY_NEXT_SOURCE = {VIDEO_UNAVAILABLE, NO_USEFUL_COMMENTARY}


@dataclass(slots=True)
class YouTubeOutcome:
    title: str
    status: str = UNCHANGED
    video_id: str | None = None
    search_called: bool = False
    gemini_called: bool = False
    error: str | None = None

    def as_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {"title": self.title, "status": self.status}
        if self.video_id:
            details["video_id"] = self.video_id
        if self.search_called:
            details["search_called"] = True
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
    """Provider clients and failure isolation for one processing run."""

    def __init__(
        self,
        db: Session,
        *,
        youtube_client: YouTubeClient | Any | None = None,
        gemini_client: GeminiClient | Any | None = None,
    ) -> None:
        self.enabled = bool(get_setting(db, "youtube.enabled"))
        self.model = str(get_setting(db, "youtube.model"))
        self.fragment_minutes = int(get_setting(db, "youtube.fragment_minutes"))
        self.max_games = int(get_setting(db, "youtube.max_games_per_run"))
        self.disabled_reason: str | None = None
        self.youtube_disabled_reason: str | None = None
        self.gemini_disabled_reason: str | None = None
        self.search_calls = 0
        self.gemini_calls = 0
        self._youtube = youtube_client
        self._gemini = gemini_client

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
        if self._youtube is not None and hasattr(self._youtube, "close"):
            self._youtube.close()

    def youtube_client(self) -> YouTubeClient:
        if self._youtube is None:
            self._youtube = YouTubeClient()
        return self._youtube

    def gemini_client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient(model=self.model)
        return self._gemini

    def enrich_game(self, db: Session, game: Game) -> YouTubeOutcome:
        """Run at most one search and one logical video analysis for a game."""
        outcome = YouTubeOutcome(title=game.title)
        if not self.enabled or self.disabled_reason:
            outcome.error = self.disabled_reason
            return outcome

        row = db.scalar(select(YouTubeAnalysis).where(YouTubeAnalysis.game_id == game.id))
        if not youtube_needs_work(row):
            outcome.video_id = row.video_id if row else None
            return outcome
        if row is None:
            now = utc_now()
            row = YouTubeAnalysis(
                game_id=game.id,
                status=PENDING,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.flush()

        candidate = self._source_for_attempt(row)
        if candidate is None:
            candidate = self._discover(row, game, outcome)
            if candidate is None:
                outcome.status = row.status
                db.flush()
                return outcome
        self._apply_candidate(row, candidate)
        outcome.video_id = candidate.video_id

        duration = candidate.duration_seconds
        end_seconds = max(duration, 0)
        start_seconds = max(0, end_seconds - self.fragment_minutes * 60)
        row.fragment_start_seconds = start_seconds
        row.fragment_end_seconds = end_seconds
        row.status = PENDING
        row.status_reason = None
        row.next_retry_at = None
        row.updated_at = utc_now()

        if self.gemini_disabled_reason:
            self._failure(row, GEMINI_UNAVAILABLE, self.gemini_disabled_reason)
            outcome.status = row.status
            outcome.error = row.status_reason
            return outcome
        try:
            client = self.gemini_client()
            outcome.gemini_called = True
            self.gemini_calls += 1
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
            logger.exception("YouTube Gemini analysis crashed game=%s", game.id)
            self._failure(row, GEMINI_FAILED, f"{type(exc).__name__}: {exc}")
            outcome.error = row.status_reason
        else:
            now = utc_now()
            validated_liked = [
                statement
                for statement, evidence in zip(result.liked, result.liked_evidence, strict=False)
                if _evidence_is_in_transcript(evidence, result.speech_transcript)
            ]
            validated_disliked = [
                statement
                for statement, evidence in zip(
                    result.disliked, result.disliked_evidence, strict=False
                )
                if _evidence_is_in_transcript(evidence, result.speech_transcript)
            ]
            row.speech_transcript = result.speech_transcript
            row.summary = result.overall_impression
            row.liked = validated_liked
            row.disliked = validated_disliked
            row.analysis_data = {
                "prompt_version": result.prompt_version,
                "has_useful_commentary": result.has_useful_commentary,
                "speech_transcript": result.speech_transcript,
                "overall_opinion_evidence": result.overall_opinion_evidence,
                "overall_impression": result.overall_impression,
                "liked": [
                    {"statement": statement, "speech_evidence": evidence}
                    for statement, evidence in zip(
                        result.liked, result.liked_evidence, strict=False
                    )
                ],
                "disliked": [
                    {"statement": statement, "speech_evidence": evidence}
                    for statement, evidence in zip(
                        result.disliked, result.disliked_evidence, strict=False
                    )
                ],
                "validated_liked": validated_liked,
                "validated_disliked": validated_disliked,
            }
            row.model_name = result.model
            row.analyzed_at = now
            row.updated_at = now
            useful = (
                result.has_useful_commentary
                and bool(result.overall_impression)
                and _evidence_is_in_transcript(
                    result.overall_opinion_evidence, result.speech_transcript
                )
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
            outcome.status = row.status

        outcome.status = row.status
        db.flush()
        return outcome

    def _source_for_attempt(self, row: YouTubeAnalysis) -> VideoCandidate | None:
        if row.video_id and row.status in _RETRY_SAME_SOURCE:
            return _candidate_from_row(row)
        if row.status in _TRY_NEXT_SOURCE:
            self._mark_attempted(row)
            return _next_cached_candidate(row)
        return None

    def _discover(
        self, row: YouTubeAnalysis, game: Game, outcome: YouTubeOutcome
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
            result = client.search_game(game.title)
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
            row.speech_transcript = None
            row.summary = None
            row.liked = None
            row.disliked = None
            row.analysis_data = None
            row.model_name = None
            row.analyzed_at = None

    def _failure(self, row: YouTubeAnalysis, status: str, reason: str) -> None:
        now = utc_now()
        row.status = status
        row.status_reason = reason[:1000]
        row.next_retry_at = now + timedelta(hours=settings.youtube_retry_interval_hours)
        row.updated_at = now

    def _mark_attempted(self, row: YouTubeAnalysis) -> None:
        if not row.video_id:
            return
        data = dict(row.search_data or {})
        attempted = list(data.get("attempted_video_ids") or [])
        if row.video_id not in attempted:
            attempted.append(row.video_id)
        data["attempted_video_ids"] = attempted
        row.search_data = data


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

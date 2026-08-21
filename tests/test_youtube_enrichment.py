"""Durable YouTube state, retries and provider isolation."""

from datetime import timedelta

from sqlalchemy import select

from app.collectors.gemini import GeminiTemporaryError, YouTubeVideoResult
from app.collectors.youtube import (
    VideoCandidate,
    YouTubeSearchResult,
    YouTubeTemporaryError,
)
from app.db import SessionLocal
from app.models import Game, YouTubeAnalysis
from app.services.games import apply_game_snapshot
from app.services.youtube import (
    GEMINI_FAILED,
    NO_CANDIDATE,
    NO_USEFUL_COMMENTARY,
    SUCCESS,
    UNCHANGED,
    YOUTUBE_ERROR,
    YouTubeEnrichmentSession,
)
from app.time import as_utc, utc_now
from tests.conftest import build_snapshot


def candidate(video_id: str, *, views: int = 1000, duration: int = 2400) -> VideoCandidate:
    return VideoCandidate(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"Test Game Let's Play Part {video_id}",
        channel_id=f"channel-{video_id}",
        channel_title=f"Creator {video_id}",
        published_at=utc_now() - timedelta(days=10),
        view_count=views,
        duration_seconds=duration,
    )


class FakeYouTube:
    def __init__(self, candidates=None, error: Exception | None = None) -> None:
        self.candidates = list(candidates or [])
        self.error = error
        self.calls: list[str] = []
        self.closed = False

    def search_game(self, title: str) -> YouTubeSearchResult:
        self.calls.append(title)
        if self.error:
            raise self.error
        return YouTubeSearchResult(query=f'"{title}" gameplay', candidates=self.candidates)

    def close(self) -> None:
        self.closed = True


class FakeVideoGemini:
    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.results = list(results or [video_result()])
        self.error = error
        self.calls: list[dict] = []

    def analyze_youtube_video(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.results.pop(0)


def video_result(*, useful: bool = True, impression: str = "The creator likes the combat"):
    return YouTubeVideoResult(
        has_useful_commentary=useful,
        speech_transcript="I really like how deliberate these fights feel.",
        overall_opinion_evidence=(
            "I really like how deliberate these fights feel" if useful else ""
        ),
        overall_impression=impression if useful else "",
        liked=["Combat rewards deliberate timing"] if useful else [],
        disliked=[],
        liked_evidence=["I really like how deliberate these fights feel"] if useful else [],
        disliked_evidence=[],
        model="test-video-model",
    )


def store_game(slug: str = "test-game") -> Game:
    with SessionLocal() as db:
        game = apply_game_snapshot(db, build_snapshot(slug, title="Test Game", reviews=0))
        db.commit()
        return game


def run(game_id, youtube: FakeYouTube, gemini: FakeVideoGemini):
    with SessionLocal() as db:
        game = db.get(Game, game_id)
        session = YouTubeEnrichmentSession(db, youtube_client=youtube, gemini_client=gemini)
        outcome = session.enrich_game(db, game)
        db.commit()
        return outcome


def row_for(game_id) -> YouTubeAnalysis:
    with SessionLocal() as db:
        row = db.scalar(select(YouTubeAnalysis).where(YouTubeAnalysis.game_id == game_id))
        assert row is not None
        db.expunge(row)
        return row


def test_search_metadata_fragment_and_structured_result_are_persisted() -> None:
    game = store_game()
    source = candidate("popular", views=4_200_000, duration=2700)
    youtube = FakeYouTube([source])
    gemini = FakeVideoGemini()

    outcome = run(game.id, youtube, gemini)

    assert outcome.status == SUCCESS
    assert youtube.calls == ["Test Game"]
    assert gemini.calls[0]["start_seconds"] == 1800
    assert gemini.calls[0]["end_seconds"] == 2700
    row = row_for(game.id)
    assert row.video_id == "popular"
    assert row.video_url == source.url
    assert row.view_count == 4_200_000
    assert row.duration_seconds == 2700
    assert row.fragment_start_seconds == 1800
    assert row.fragment_end_seconds == 2700
    assert row.speech_transcript.startswith("I really like")
    assert row.summary == "The creator likes the combat"
    assert row.liked == ["Combat rewards deliberate timing"]
    assert row.model_name == "test-video-model"
    assert row.analysis_data["prompt_version"] == "4"


def test_successful_repeat_makes_no_external_requests() -> None:
    game = store_game()
    run(game.id, FakeYouTube([candidate("stable")]), FakeVideoGemini())
    youtube = FakeYouTube([candidate("unused")])
    gemini = FakeVideoGemini()

    outcome = run(game.id, youtube, gemini)

    assert outcome.status == UNCHANGED
    assert youtube.calls == []
    assert gemini.calls == []


def test_no_result_is_cached_and_not_researched_on_the_next_run() -> None:
    game = store_game()
    first = FakeYouTube([])

    outcome = run(game.id, first, FakeVideoGemini())
    assert outcome.status == NO_CANDIDATE
    assert first.calls == ["Test Game"]

    second = FakeYouTube([candidate("late")])
    repeated = run(game.id, second, FakeVideoGemini())
    assert repeated.status == UNCHANGED
    assert second.calls == []
    assert as_utc(row_for(game.id).next_retry_at) > utc_now()


def test_source_without_opinion_advances_through_cached_candidates_without_new_search() -> None:
    game = store_game()
    first_source = candidate("first", views=2000)
    second_source = candidate("second", views=1000)
    first_youtube = FakeYouTube([first_source, second_source])

    first = run(
        game.id,
        first_youtube,
        FakeVideoGemini(results=[video_result(useful=False)]),
    )
    assert first.status == NO_USEFUL_COMMENTARY
    with SessionLocal() as db:
        row = db.scalar(select(YouTubeAnalysis).where(YouTubeAnalysis.game_id == game.id))
        row.next_retry_at = utc_now() - timedelta(seconds=1)
        db.commit()

    second_youtube = FakeYouTube([])
    second_gemini = FakeVideoGemini()
    second = run(game.id, second_youtube, second_gemini)

    assert second.status == SUCCESS
    assert second.video_id == "second"
    assert second_youtube.calls == []
    assert second_gemini.calls[0]["video_url"].endswith("second")


def test_unverifiable_opinion_evidence_is_not_persisted_as_fact() -> None:
    game = store_game()
    result = video_result()
    result.liked_evidence = ["Words the creator never said"]

    outcome = run(
        game.id,
        FakeYouTube([candidate("evidence")]),
        FakeVideoGemini(results=[result]),
    )

    assert outcome.status == SUCCESS  # the separately evidenced overall verdict is valid
    row = row_for(game.id)
    assert row.liked == []
    assert row.analysis_data["liked"][0]["speech_evidence"] == "Words the creator never said"
    assert row.analysis_data["validated_liked"] == []


def test_missing_overall_quote_marks_the_source_as_not_useful() -> None:
    game = store_game()
    result = video_result()
    result.overall_opinion_evidence = "A broad opinion absent from the transcript"

    outcome = run(
        game.id,
        FakeYouTube([candidate("unsupported")]),
        FakeVideoGemini(results=[result]),
    )

    assert outcome.status == NO_USEFUL_COMMENTARY


def test_youtube_and_gemini_errors_get_distinct_retryable_statuses() -> None:
    search_game = store_game("search-error")
    search = run(
        search_game.id,
        FakeYouTube(error=YouTubeTemporaryError("503 unavailable")),
        FakeVideoGemini(),
    )
    assert search.status == YOUTUBE_ERROR
    assert "503" in search.error

    gemini_game = store_game("gemini-error")
    gemini = run(
        gemini_game.id,
        FakeYouTube([candidate("source")]),
        FakeVideoGemini(error=GeminiTemporaryError("503 overloaded")),
    )
    assert gemini.status == GEMINI_FAILED
    assert "503" in gemini.error
    assert as_utc(row_for(gemini_game.id).next_retry_at) > utc_now()

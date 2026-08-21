"""Durable YouTube state, the subtitle path, its fallback, retries and budgets."""

from datetime import timedelta

from sqlalchemy import select

from app.collectors.gemini import GeminiTemporaryError, YouTubeVideoResult
from app.collectors.transcript import (
    Cue,
    TranscriptTemporaryError,
    TranscriptTrack,
    TranscriptUnavailable,
)
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
    NO_TRANSCRIPT,
    NO_USEFUL_COMMENTARY,
    SEARCH_BUDGET,
    SUCCESS,
    TRANSCRIPT_ERROR,
    UNCHANGED,
    YOUTUBE_ERROR,
    YouTubeEnrichmentSession,
)
from app.time import as_utc, utc_now
from tests.conftest import build_snapshot

TAIL_SPEECH = "I really like how deliberate these fights feel"


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


class FakeTranscripts:
    """Subtitles per video id; anything not listed publishes no captions."""

    def __init__(self, tracks: dict[str, TranscriptTrack] | None = None, error=None) -> None:
        self.tracks = dict(tracks or {})
        self.error = error
        self.calls: list[str] = []

    def fetch(self, video_id: str) -> TranscriptTrack:
        self.calls.append(video_id)
        if self.error:
            raise self.error
        if video_id not in self.tracks:
            raise TranscriptUnavailable("The video publishes no subtitles or automatic captions")
        return self.tracks[video_id]

    def close(self) -> None:
        return None


class FakeGemini:
    def __init__(self, results=None, error: Exception | None = None, method: str = "transcript"):
        self.results = list(results or [])
        self.error = error
        self.calls: list[dict] = []
        self._method = method

    def _answer(self, kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.results.pop(0) if self.results else analysis_result()

    def analyze_letsplay_transcript(self, **kwargs):
        return self._answer(kwargs)

    def analyze_youtube_video(self, **kwargs):
        return self._answer(kwargs)


def spoken_track(video_id: str = "video", *, duration: int = 2400) -> TranscriptTrack:
    cues = [
        Cue(start=float(second), end=float(second) + 5.0, text=TAIL_SPEECH)
        for second in range(0, duration, 20)
    ]
    return TranscriptTrack(
        video_id=video_id,
        language="en",
        is_automatic=True,
        duration_seconds=float(duration),
        cues=cues,
    )


def analysis_result(
    *,
    useful: bool = True,
    impression: str = "Автор хвалит боевую систему",
    source: str = "transcript",
    model: str = "test-transcript-model",
) -> YouTubeVideoResult:
    return YouTubeVideoResult(
        has_useful_commentary=useful,
        speech_transcript=TAIL_SPEECH,
        overall_opinion_evidence=TAIL_SPEECH if useful else "",
        overall_impression=impression if useful else "",
        liked=["Бои вознаграждают точный тайминг"] if useful else [],
        disliked=[],
        liked_evidence=[TAIL_SPEECH] if useful else [],
        disliked_evidence=[],
        model=model,
        source=source,
    )


def store_game(slug: str = "test-game") -> Game:
    with SessionLocal() as db:
        game = apply_game_snapshot(db, build_snapshot(slug, title="Test Game", reviews=0))
        db.commit()
        return game


def run(game_id, youtube, transcripts, gemini, video_gemini=None, session=None):
    with SessionLocal() as db:
        game = db.get(Game, game_id)
        session = session or YouTubeEnrichmentSession(
            db,
            youtube_client=youtube,
            transcript_client=transcripts,
            gemini_client=gemini,
            video_gemini_client=video_gemini or FakeGemini(method="video"),
        )
        outcome = session.enrich_game(db, game)
        db.commit()
        return outcome


def row_for(game_id) -> YouTubeAnalysis:
    with SessionLocal() as db:
        row = db.scalar(select(YouTubeAnalysis).where(YouTubeAnalysis.game_id == game_id))
        assert row is not None
        db.expunge(row)
        return row


def expire_retry(game_id) -> None:
    with SessionLocal() as db:
        row = db.scalar(select(YouTubeAnalysis).where(YouTubeAnalysis.game_id == game_id))
        row.next_retry_at = utc_now() - timedelta(seconds=1)
        db.commit()


def test_subtitles_are_the_main_path_and_no_video_is_ever_sent() -> None:
    game = store_game()
    source = candidate("popular", views=4_200_000, duration=2700)
    youtube = FakeYouTube([source])
    transcripts = FakeTranscripts({"popular": spoken_track("popular", duration=2700)})
    gemini = FakeGemini()
    video = FakeGemini(method="video")

    outcome = run(game.id, youtube, transcripts, gemini, video)

    assert outcome.status == SUCCESS
    assert outcome.source == "transcript"
    assert youtube.calls == ["Test Game"]
    assert transcripts.calls == ["popular"]
    assert video.calls == []  # the multimodal fallback is untouched
    assert gemini.calls[0]["language"] == "en"
    assert gemini.calls[0]["end_seconds"] == 2700

    row = row_for(game.id)
    assert row.video_id == "popular"
    assert row.analysis_source == "transcript"
    assert row.transcript_language == "en"
    assert row.transcript_is_automatic is True
    assert row.fragment_start_seconds == 1800
    assert row.fragment_end_seconds == 2700
    assert row.summary == "Автор хвалит боевую систему"
    assert row.liked == ["Бои вознаграждают точный тайминг"]
    assert row.model_name == "test-transcript-model"
    assert row.analysis_data["prompt_version"] == "5"
    assert row.analysis_data["words_per_minute"] > 0


def test_the_analyzed_transcript_is_the_fetched_one_not_a_model_retelling() -> None:
    """Evidence is checked against captions the model never had a chance to rewrite."""
    game = store_game()
    result = analysis_result()
    result.speech_transcript = "something the model made up instead"
    transcripts = FakeTranscripts({"grounded": spoken_track("grounded")})

    outcome = run(game.id, FakeYouTube([candidate("grounded")]), transcripts, FakeGemini([result]))

    assert outcome.status == SUCCESS
    assert row_for(game.id).speech_transcript.startswith(TAIL_SPEECH)


def test_a_video_without_captions_falls_back_to_the_multimodal_model() -> None:
    game = store_game()
    youtube = FakeYouTube([candidate("silent", duration=1800)])
    transcripts = FakeTranscripts({})  # no captions for anything
    video = FakeGemini([analysis_result(source="video", model="test-video-model")], method="video")

    outcome = run(game.id, youtube, transcripts, FakeGemini(), video)

    assert outcome.status == SUCCESS
    assert outcome.source == "video"
    assert video.calls[0]["video_url"].endswith("silent")
    row = row_for(game.id)
    assert row.analysis_source == "video"
    assert row.model_name == "test-video-model"
    assert row.transcript_language is None


def test_captionless_candidates_are_walked_before_the_fallback_is_spent() -> None:
    game = store_game()
    sources = [candidate("first", views=3000), candidate("second", views=2000)]
    transcripts = FakeTranscripts({"second": spoken_track("second")})
    video = FakeGemini(method="video")

    outcome = run(game.id, FakeYouTube(sources), transcripts, FakeGemini(), video)

    assert outcome.status == SUCCESS
    assert outcome.video_id == "second"
    assert transcripts.calls == ["first", "second"]
    assert video.calls == []


def test_an_exhausted_fallback_budget_leaves_a_retryable_no_transcript_state() -> None:
    game = store_game()
    with SessionLocal() as db:
        session = YouTubeEnrichmentSession(
            db,
            youtube_client=FakeYouTube([candidate("silent")]),
            transcript_client=FakeTranscripts({}),
            gemini_client=FakeGemini(),
            video_gemini_client=FakeGemini(method="video"),
        )
        session.max_video_fallbacks = 0
        outcome = session.enrich_game(db, db.get(Game, game.id))
        db.commit()

    assert outcome.status == NO_TRANSCRIPT
    assert as_utc(row_for(game.id).next_retry_at) > utc_now()


def test_successful_repeat_makes_no_external_requests() -> None:
    game = store_game()
    run(
        game.id,
        FakeYouTube([candidate("stable")]),
        FakeTranscripts({"stable": spoken_track("stable")}),
        FakeGemini(),
    )
    youtube = FakeYouTube([candidate("unused")])
    transcripts = FakeTranscripts({})
    gemini = FakeGemini()

    outcome = run(game.id, youtube, transcripts, gemini)

    assert outcome.status == UNCHANGED
    assert youtube.calls == []
    assert transcripts.calls == []
    assert gemini.calls == []


def test_no_result_is_cached_and_not_researched_on_the_next_run() -> None:
    game = store_game()
    first = FakeYouTube([])

    outcome = run(game.id, first, FakeTranscripts(), FakeGemini())
    assert outcome.status == NO_CANDIDATE
    assert first.calls == ["Test Game"]

    second = FakeYouTube([candidate("late")])
    repeated = run(game.id, second, FakeTranscripts(), FakeGemini())
    assert repeated.status == UNCHANGED
    assert second.calls == []
    assert as_utc(row_for(game.id).next_retry_at) > utc_now()


def test_source_without_opinion_advances_through_cached_candidates_without_new_search() -> None:
    game = store_game()
    sources = [candidate("first", views=2000), candidate("second", views=1000)]
    transcripts = FakeTranscripts(
        {"first": spoken_track("first"), "second": spoken_track("second")}
    )

    first = run(
        game.id,
        FakeYouTube(sources),
        transcripts,
        FakeGemini([analysis_result(useful=False)]),
    )
    assert first.status == NO_USEFUL_COMMENTARY
    expire_retry(game.id)

    second_youtube = FakeYouTube([])
    second_gemini = FakeGemini()
    second = run(game.id, second_youtube, transcripts, second_gemini)

    assert second.status == SUCCESS
    assert second.video_id == "second"
    assert second_youtube.calls == []


def test_unverifiable_opinion_evidence_is_not_persisted_as_fact() -> None:
    game = store_game()
    result = analysis_result()
    result.liked_evidence = ["Words the creator never said"]

    outcome = run(
        game.id,
        FakeYouTube([candidate("evidence")]),
        FakeTranscripts({"evidence": spoken_track("evidence")}),
        FakeGemini([result]),
    )

    assert outcome.status == SUCCESS  # the separately evidenced overall verdict is valid
    row = row_for(game.id)
    assert row.liked == []
    assert row.analysis_data["liked"][0]["speech_evidence"] == "Words the creator never said"
    assert row.analysis_data["validated_liked"] == []


def test_missing_overall_quote_marks_the_source_as_not_useful() -> None:
    game = store_game()
    result = analysis_result()
    result.overall_opinion_evidence = "A broad opinion absent from the transcript"

    outcome = run(
        game.id,
        FakeYouTube([candidate("unsupported")]),
        FakeTranscripts({"unsupported": spoken_track("unsupported")}),
        FakeGemini([result]),
    )

    assert outcome.status == NO_USEFUL_COMMENTARY


def test_provider_errors_get_distinct_retryable_statuses() -> None:
    search_game = store_game("search-error")
    search = run(
        search_game.id,
        FakeYouTube(error=YouTubeTemporaryError("503 unavailable")),
        FakeTranscripts(),
        FakeGemini(),
    )
    assert search.status == YOUTUBE_ERROR
    assert "503" in search.error

    transcript_game = store_game("transcript-error")
    transcript = run(
        transcript_game.id,
        FakeYouTube([candidate("source")]),
        FakeTranscripts(error=TranscriptTemporaryError("yt-dlp timed out")),
        FakeGemini(),
    )
    assert transcript.status == TRANSCRIPT_ERROR
    assert as_utc(row_for(transcript_game.id).next_retry_at) > utc_now()

    gemini_game = store_game("gemini-error")
    gemini = run(
        gemini_game.id,
        FakeYouTube([candidate("source")]),
        FakeTranscripts({"source": spoken_track("source")}),
        FakeGemini(error=GeminiTemporaryError("503 overloaded")),
    )
    assert gemini.status == GEMINI_FAILED
    assert "503" in gemini.error
    assert as_utc(row_for(gemini_game.id).next_retry_at) > utc_now()


def test_the_search_budget_defers_a_game_instead_of_failing_it() -> None:
    """A run out of Data API units must not burn the game's retry window."""
    first = store_game("budget-one")
    second = store_game("budget-two")
    youtube = FakeYouTube([candidate("only")])
    transcripts = FakeTranscripts({"only": spoken_track("only")})

    with SessionLocal() as db:
        session = YouTubeEnrichmentSession(
            db,
            youtube_client=youtube,
            transcript_client=transcripts,
            gemini_client=FakeGemini(),
            video_gemini_client=FakeGemini(method="video"),
        )
        session.max_searches = 1
        assert session.enrich_game(db, db.get(Game, first.id)).status == SUCCESS
        deferred = session.enrich_game(db, db.get(Game, second.id))
        db.commit()

    assert deferred.status == SEARCH_BUDGET
    assert youtube.calls == ["Test Game"]
    assert row_for(second.id).next_retry_at is None  # picked up again by the next run

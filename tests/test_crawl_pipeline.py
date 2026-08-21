"""Crawl scheduling rules and the shared processing path."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.collectors.gemini import GeminiTemporaryError, GeminiUnavailable
from app.collectors.youtube import YouTubeTemporaryError
from app.db import SessionLocal
from app.models import (
    CrawlStatus,
    DailyCrawlState,
    DailyProcessedGame,
    Game,
    GamePlatform,
    GameReview,
    ProcessingRun,
    ReviewSummary,
    RunStatus,
    RunTrigger,
)
from app.services import crawl, pipeline
from app.services.crawl import STAGE_BROWSE, STAGE_NEW_RELEASES
from app.services.enrichment import EnrichmentSession
from app.services.games import apply_game_snapshot
from app.services.pipeline import execute_run
from app.services.runs import enqueue_manual_run, enqueue_scheduled_run
from app.services.youtube import YouTubeEnrichmentSession
from tests.conftest import StubGeminiClient, StubMetacriticClient, build_snapshot

DAY_ONE = date(2026, 8, 21)
DAY_TWO = date(2026, 8, 22)


@pytest.fixture
def frozen_day(monkeypatch: pytest.MonkeyPatch) -> "DayClock":
    clock = DayClock(DAY_ONE)
    monkeypatch.setattr(crawl, "app_today", clock.today)
    return clock


class DayClock:
    def __init__(self, current: date) -> None:
        self.current = current

    def today(self) -> date:
        return self.current

    def advance(self, days: int = 1) -> None:
        self.current += timedelta(days=days)


def run_batch(client: StubMetacriticClient, trigger: RunTrigger = RunTrigger.MANUAL) -> dict:
    """Execute one run exactly as the worker does and return its final row as data."""
    with SessionLocal() as db:
        run = (
            enqueue_manual_run(db, None)
            if trigger is RunTrigger.MANUAL
            else enqueue_scheduled_run(db)
        )
        run.status = RunStatus.RUNNING
        db.commit()
        execute_run(db, run, client)
        db.refresh(run)
        return {
            "id": run.id,
            "status": run.status,
            "message": run.message,
            "error": run.error,
            "progress_current": run.progress_current,
            "progress_total": run.progress_total,
            "details": dict(run.details or {}),
        }


def game_titles() -> list[str]:
    with SessionLocal() as db:
        return list(db.scalars(select(Game.title).order_by(Game.title)))


def game_slugs() -> set[str]:
    with SessionLocal() as db:
        keys = db.scalars(select(Game.source_key)).all()
    return {key.split(":", 1)[1] for key in keys}


def crawl_cursor(day: date) -> dict:
    with SessionLocal() as db:
        state = db.scalar(select(DailyCrawlState).where(DailyCrawlState.processing_date == day))
        assert state is not None
        return dict(state.cursor or {})


def stub(**kwargs: object) -> StubMetacriticClient:
    defaults = {
        "new_releases": [f"new-{index}" for index in range(1, 21)],
        "browse_pages": {
            1: [f"browse-{index}" for index in range(1, 25)],
            2: [f"browse-page2-{index}" for index in range(1, 25)],
        },
    }
    defaults.update(kwargs)
    return StubMetacriticClient(**defaults)  # type: ignore[arg-type]


def test_first_run_of_the_day_takes_twenty_new_releases(frozen_day: DayClock) -> None:
    client = stub()

    result = run_batch(client)

    assert client.collected == [f"new-{index}" for index in range(1, 21)]
    assert result["status"] is RunStatus.SUCCEEDED
    assert result["progress_current"] == 20
    assert result["progress_total"] == 20
    assert result["details"]["stage"] == STAGE_NEW_RELEASES
    assert len(game_slugs()) == 20
    assert crawl_cursor(DAY_ONE)["stage"] == STAGE_BROWSE


def test_later_runs_of_the_day_continue_through_browse_pages(frozen_day: DayClock) -> None:
    first = stub()
    run_batch(first)

    second = stub()
    result = run_batch(second)

    assert second.collected == [f"browse-{index}" for index in range(1, 21)]
    assert result["details"]["stage"] == STAGE_BROWSE
    cursor = crawl_cursor(DAY_ONE)
    assert cursor["browse_page"] == 1
    assert cursor["browse_offset"] == 20

    third = stub()
    run_batch(third)

    # The remaining four of page one, then page two continues the traversal.
    assert third.collected[:4] == [f"browse-{index}" for index in range(21, 25)]
    assert third.collected[4] == "browse-page2-1"
    assert crawl_cursor(DAY_ONE)["browse_page"] == 2


def test_a_game_is_never_processed_twice_on_the_same_day(frozen_day: DayClock) -> None:
    run_batch(stub())

    # The browse listing repeats games that the New Releases stage already handled.
    repeated = stub(
        browse_pages={
            1: [f"new-{index}" for index in range(1, 21)] + ["browse-a", "browse-b"],
            2: ["browse-c"],
        }
    )
    run_batch(repeated)

    assert repeated.collected == ["browse-a", "browse-b", "browse-c"]
    with SessionLocal() as db:
        processed = db.scalar(
            select(func.count())
            .select_from(DailyProcessedGame)
            .where(DailyProcessedGame.processing_date == DAY_ONE)
        )
    assert processed == 23


def test_a_new_calendar_day_restarts_from_new_releases(frozen_day: DayClock) -> None:
    run_batch(stub())
    run_batch(stub())
    assert crawl_cursor(DAY_ONE)["stage"] == STAGE_BROWSE

    frozen_day.advance()
    client = stub()
    result = run_batch(client)

    assert result["details"]["stage"] == STAGE_NEW_RELEASES
    assert client.collected == [f"new-{index}" for index in range(1, 21)]
    assert crawl_cursor(DAY_TWO)["stage"] == STAGE_BROWSE
    assert crawl_cursor(DAY_ONE)["stage"] == STAGE_BROWSE


def test_known_game_seen_again_is_updated_not_duplicated(frozen_day: DayClock) -> None:
    run_batch(
        stub(
            new_releases=["shared-game"],
            snapshots={"shared-game": build_snapshot("shared-game", title="Shared Game")},
        )
    )

    frozen_day.advance()
    refreshed = build_snapshot(
        "shared-game",
        title="Shared Game: Definitive Edition",
        metascore=91,
        userscore="8.9",
        reviews=3,
    )
    run_batch(stub(new_releases=["shared-game"], snapshots={"shared-game": refreshed}))

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Game)) == 1
        game = db.scalar(select(Game))
        assert game.title == "Shared Game: Definitive Edition"
        assert db.scalar(select(func.count()).select_from(GamePlatform)) == 1
        platform = db.scalar(select(GamePlatform))
        assert platform.metascore == 91
        assert platform.userscore == Decimal("8.9")
        # Two reviews were re-collected verbatim and one is new.
        assert db.scalar(select(func.count()).select_from(GameReview)) == 6


def test_one_failing_game_does_not_lose_the_rest_of_the_batch(frozen_day: DayClock) -> None:
    client = stub(new_releases=["good-1", "broken", "good-2"], failing_slugs={"broken"})

    result = run_batch(client)

    assert result["status"] is RunStatus.SUCCEEDED
    assert result["message"] == "Processed 2 of 3 games, 1 failed"
    assert result["details"]["failed"] == 1
    assert result["details"]["errors"][0]["slug"] == "broken"
    assert result["error"] and "503" in result["error"]
    assert game_slugs() == {"good-1", "good-2"}
    assert crawl_cursor(DAY_ONE)["failed_slugs"] == ["broken"]

    # The failed game is not retried again during the same day.
    retry = stub(browse_pages={1: ["broken", "browse-x"]})
    run_batch(retry)
    assert retry.collected == ["browse-x"]


def test_a_batch_where_every_game_fails_is_reported_as_failed(frozen_day: DayClock) -> None:
    client = stub(new_releases=["broken-1", "broken-2"], failing_slugs={"broken-1", "broken-2"})

    result = run_batch(client)

    assert result["status"] is RunStatus.FAILED
    assert result["message"] == "All 2 games failed"
    with SessionLocal() as db:
        state = db.scalar(select(DailyCrawlState))
        assert state.status is CrawlStatus.FAILED


def test_discovery_failure_fails_the_run_instead_of_reporting_empty_success(
    frozen_day: DayClock,
) -> None:
    result = run_batch(stub(discovery_error="GET /game/ returned 503"))

    assert result["status"] is RunStatus.FAILED
    assert "503" in (result["error"] or "")
    assert game_titles() == []


def test_exhausted_listing_reports_a_clean_no_work_result(frozen_day: DayClock) -> None:
    run_batch(stub())
    result = run_batch(stub(browse_pages={}))

    assert result["status"] is RunStatus.SUCCEEDED
    assert result["message"] == "No unprocessed games left for today"
    assert result["progress_total"] == 0


def test_progress_and_current_game_are_published_during_the_run(frozen_day: DayClock) -> None:
    seen: list[tuple[int, int | None, str | None]] = []

    class ObservingClient(StubMetacriticClient):
        def collect_game(self, slug: str):
            with SessionLocal() as observer:
                run = observer.scalar(select(ProcessingRun))
                seen.append((run.progress_current, run.progress_total, run.message))
            return super().collect_game(slug)

    client = ObservingClient(new_releases=["alpha", "beta"])
    run_batch(client)

    assert seen[0] == (0, 2, "Collecting alpha (1/2)")
    assert seen[1] == (1, 2, "Collecting beta (2/2)")


def rich_stub(*slugs: str) -> StubMetacriticClient:
    """Games with enough reviews per audience to be worth a summary."""
    return stub(
        new_releases=list(slugs),
        snapshots={slug: build_snapshot(slug, reviews=4) for slug in slugs},
    )


def test_ai_enrichment_runs_inside_the_batch_and_extends_progress(
    frozen_day: DayClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemini = StubGeminiClient()
    monkeypatch.setattr(
        pipeline, "EnrichmentSession", lambda db: EnrichmentSession(db, client=gemini)
    )

    result = run_batch(rich_stub("alpha", "beta"))

    assert result["status"] is RunStatus.SUCCEEDED
    assert result["message"] == "Processed 2 games · AI: 2 enriched"
    assert result["progress_total"] == 4  # two collected, then two analysed
    assert result["progress_current"] == 4
    ai = result["details"]["ai"]
    assert ai == {
        "enabled": True,
        "model": "test-model",
        "planned": 2,
        "generated": 2,
        "failed": 0,
        "skipped": 0,
        "calls": 4,
        "games": [
            {
                "title": "Alpha",
                "summaries": {"critics": "generated", "users": "generated"},
                "tags": "generated",
            },
            {
                "title": "Beta",
                "summaries": {"critics": "generated", "users": "generated"},
                "tags": "generated",
            },
        ],
    }
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReviewSummary)) == 4


def test_a_gemini_failure_does_not_fail_the_crawl(
    frozen_day: DayClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemini = StubGeminiClient(review_error=GeminiTemporaryError("503 model overloaded"))
    monkeypatch.setattr(
        pipeline, "EnrichmentSession", lambda db: EnrichmentSession(db, client=gemini)
    )

    result = run_batch(rich_stub("alpha", "beta"))

    assert result["status"] is RunStatus.SUCCEEDED
    assert "2 AI failures" in result["message"]
    assert result["details"]["ai"]["failed"] == 2
    assert len(gemini.review_calls) == 2  # the second game was still attempted
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Game)) == 2
        assert db.scalar(select(func.count()).select_from(ReviewSummary)) == 0


def test_a_rejected_key_stops_enrichment_but_keeps_the_crawl_result(
    frozen_day: DayClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    gemini = StubGeminiClient(review_error=GeminiUnavailable("403 API key not valid"))
    monkeypatch.setattr(
        pipeline, "EnrichmentSession", lambda db: EnrichmentSession(db, client=gemini)
    )

    result = run_batch(rich_stub("alpha", "beta", "gamma"))

    assert result["status"] is RunStatus.SUCCEEDED
    ai = result["details"]["ai"]
    assert "403" in ai["disabled_reason"]
    assert ai["failed"] == 1
    assert ai["skipped"] == 2
    assert len(gemini.review_calls) == 1  # no further calls after the fatal answer
    assert len(game_slugs()) == 3


def test_enrichment_is_absent_from_the_run_when_no_key_is_configured(
    frozen_day: DayClock,
) -> None:
    result = run_batch(rich_stub("alpha"))

    ai = result["details"]["ai"]
    assert ai["enabled"] is False
    assert ai["disabled_reason"] == "GEMINI_API_KEY is not configured"
    assert result["message"] == "Processed 1 games"


def test_youtube_failures_do_not_change_the_crawl_or_review_enrichment_result(
    frozen_day: DayClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingYouTube:
        def search_game(self, _title: str):
            raise YouTubeTemporaryError("503 YouTube unavailable")

        def close(self) -> None:
            pass

    gemini = StubGeminiClient()
    monkeypatch.setattr(
        pipeline, "EnrichmentSession", lambda db: EnrichmentSession(db, client=gemini)
    )
    monkeypatch.setattr(
        pipeline,
        "YouTubeEnrichmentSession",
        lambda db: YouTubeEnrichmentSession(
            db, youtube_client=FailingYouTube(), gemini_client=object()
        ),
    )

    result = run_batch(rich_stub("alpha", "beta"))

    assert result["status"] is RunStatus.SUCCEEDED
    assert result["details"]["ai"]["generated"] == 2
    assert result["details"]["youtube"]["failed"] == 2
    assert result["details"]["youtube"]["search_calls"] == 2
    assert "YouTube: 2 failed" in result["message"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReviewSummary)) == 4


def test_reviewed_games_take_the_youtube_budget_before_unreviewed_indies(
    frozen_day: DayClock,
) -> None:
    """A catalogue of fresh indies would otherwise spend every run on titles with no videos."""
    from tests.test_youtube_enrichment import FakeGemini, FakeTranscripts, FakeYouTube

    with SessionLocal() as db:
        # Stored newest-first the other way round, so recency alone would pick the indie.
        apply_game_snapshot(db, build_snapshot("reviewed-game", reviews=12))
        apply_game_snapshot(db, build_snapshot("indie-game", reviews=0))
        db.commit()
        run = enqueue_manual_run(db, None)
        run.status = RunStatus.RUNNING
        db.commit()

        session = YouTubeEnrichmentSession(
            db,
            youtube_client=FakeYouTube([]),
            transcript_client=FakeTranscripts(),
            gemini_client=FakeGemini(),
            video_gemini_client=FakeGemini(),
        )
        session.max_games = 1
        details = pipeline.enrich_youtube_games(db, run, [], offset=0, session=session)

    assert [item["title"] for item in details["games"]] == ["Reviewed Game"]

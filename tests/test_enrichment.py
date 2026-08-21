"""Gemini enrichment: when it runs, when it stays quiet, and how it fails."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.collectors.gemini import (
    GeminiInvalidResponse,
    GeminiTemporaryError,
    GeminiUnavailable,
)
from app.db import SessionLocal
from app.models import AppSetting, Audience, Game, GameReview, ReviewSummary, Tag
from app.services import enrichment
from app.services.enrichment import (
    FAILED,
    GENERATED,
    SKIPPED_NO_REVIEWS,
    SKIPPED_TOO_FEW,
    SKIPPED_UNCHANGED,
    EnrichmentSession,
    summary_needs_refresh,
)
from app.services.games import apply_game_snapshot
from app.time import utc_now
from tests.conftest import StubGeminiClient, build_snapshot


def store(slug: str, *, critics: int = 4, users: int = 4, offset: int = 0) -> Game:
    """Persist a game with a chosen number of critic and player reviews."""
    snapshot = build_snapshot(slug, reviews=0)
    for index in range(offset, offset + critics):
        snapshot.reviews.append(
            _review("critics", f"critic:{slug}:{index}", f"Critic point {index}")
        )
    for index in range(offset, offset + users):
        snapshot.reviews.append(_review("users", f"user:{slug}:{index}", f"Player point {index}"))
    with SessionLocal() as db:
        game = apply_game_snapshot(db, snapshot)
        db.commit()
        return game


def _review(audience: str, key: str, quote: str):
    from app.collectors.metacritic import ReviewRecord

    return ReviewRecord(
        audience=audience, external_key=key, quote=quote, platform_slug="pc", publication="Site"
    )


def add_reviews(game_id, audience: Audience, keys: list[str]) -> None:
    now = utc_now()
    with SessionLocal() as db:
        for key in keys:
            db.add(
                GameReview(
                    game_id=game_id,
                    external_key=key,
                    audience=audience,
                    quote=f"Extra opinion {key}",
                    collected_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        db.commit()


def enrich(game_id, client: StubGeminiClient, session: EnrichmentSession | None = None):
    with SessionLocal() as db:
        game = db.get(Game, game_id)
        session = session or EnrichmentSession(db, client=client)
        outcome = session.enrich_game(db, game)
        db.commit()
        return outcome, session


def summaries(game_id) -> dict[Audience, ReviewSummary]:
    with SessionLocal() as db:
        return {
            row.audience: row
            for row in db.scalars(select(ReviewSummary).where(ReviewSummary.game_id == game_id))
        }


def test_reviews_are_summarized_per_audience_without_mixing() -> None:
    game = store("mixed-game", critics=4, users=6)
    client = StubGeminiClient()

    outcome, _ = enrich(game.id, client)

    assert outcome.summaries == {"critics": GENERATED, "users": GENERATED}
    assert client.review_calls == [("Mixed Game", 4, 6)]
    stored = summaries(game.id)
    assert stored[Audience.CRITICS].summary == "Summary for critics"
    assert stored[Audience.USERS].summary == "Summary for players"
    assert stored[Audience.CRITICS].source_count == 4
    assert stored[Audience.USERS].source_count == 6
    assert stored[Audience.CRITICS].positives == ["The combat rewards timing"]
    assert stored[Audience.CRITICS].model_name == "test-model"


def test_a_game_without_reviews_is_left_empty_rather_than_invented() -> None:
    game = store("quiet-game", critics=0, users=0)
    client = StubGeminiClient()

    outcome, _ = enrich(game.id, client)

    assert outcome.summaries == {"critics": SKIPPED_NO_REVIEWS, "users": SKIPPED_NO_REVIEWS}
    assert client.review_calls == []
    assert summaries(game.id) == {}
    # Tags come from description and metadata, so they are still derived.
    assert client.tag_calls == ["Quiet Game"]


def test_an_audience_below_the_threshold_is_not_summarized() -> None:
    game = store("thin-game", critics=5, users=2)
    client = StubGeminiClient()

    outcome, _ = enrich(game.id, client)

    assert outcome.summaries == {"critics": GENERATED, "users": SKIPPED_TOO_FEW}
    assert client.review_calls == [("Thin Game", 5, 0)]
    assert set(summaries(game.id)) == {Audience.CRITICS}


def test_a_second_run_without_new_reviews_asks_the_model_nothing() -> None:
    game = store("stable-game")
    first = StubGeminiClient()
    enrich(game.id, first)

    second = StubGeminiClient()
    outcome, _ = enrich(game.id, second)

    assert outcome.summaries == {"critics": SKIPPED_UNCHANGED, "users": SKIPPED_UNCHANGED}
    assert second.review_calls == []
    assert second.tag_calls == []  # metadata unchanged, so tags are not re-derived


def test_a_few_new_reviews_do_not_justify_a_new_summary() -> None:
    game = store("slow-game", critics=10, users=10)
    enrich(game.id, StubGeminiClient())

    add_reviews(game.id, Audience.CRITICS, ["critic:slow-game:extra-1"])
    client = StubGeminiClient()
    outcome, _ = enrich(game.id, client)

    assert outcome.summaries["critics"] == SKIPPED_UNCHANGED
    assert client.review_calls == []


def test_a_wave_of_new_reviews_refreshes_the_summary() -> None:
    game = store("growing-game", critics=4, users=4)
    enrich(game.id, StubGeminiClient())
    with SessionLocal() as db:
        row = db.scalar(
            select(ReviewSummary).where(
                ReviewSummary.game_id == game.id, ReviewSummary.audience == Audience.CRITICS
            )
        )
        row.generated_at = utc_now() - timedelta(days=2)
        db.commit()

    add_reviews(
        game.id, Audience.CRITICS, [f"critic:growing-game:new-{index}" for index in range(6)]
    )
    client = StubGeminiClient(liked=["Refreshed point"])
    outcome, _ = enrich(game.id, client)

    assert outcome.summaries["critics"] == GENERATED
    assert outcome.summaries["users"] == SKIPPED_UNCHANGED
    assert client.review_calls == [("Growing Game", 10, 0)]
    refreshed = summaries(game.id)[Audience.CRITICS]
    assert refreshed.source_count == 10
    assert refreshed.positives == ["Refreshed point"]


def test_the_quiet_period_holds_back_an_otherwise_due_refresh() -> None:
    game = store("fresh-game", critics=4, users=4)
    enrich(game.id, StubGeminiClient())

    add_reviews(game.id, Audience.CRITICS, [f"critic:fresh-game:new-{i}" for i in range(6)])
    client = StubGeminiClient()
    outcome, _ = enrich(game.id, client)

    assert outcome.summaries["critics"] == SKIPPED_UNCHANGED
    assert client.review_calls == []


def test_refresh_rule_requires_both_absolute_and_relative_growth() -> None:
    class Existing:
        input_digest = "old"
        source_count = 100
        generated_at = utc_now() - timedelta(days=30)

    common = {"min_reviews": 3, "min_new_reviews": 5, "min_growth": 0.25, "quiet_hours": 0}

    # 6 new reviews is enough in absolute terms but only 6% growth.
    assert not summary_needs_refresh(Existing(), 106, "new", **common)
    # 30 new reviews clears both thresholds.
    assert summary_needs_refresh(Existing(), 130, "new", **common)


def test_settings_override_from_the_database_changes_the_threshold() -> None:
    game = store("tuned-game", critics=4, users=4)
    enrich(game.id, StubGeminiClient())
    now = utc_now()
    with SessionLocal() as db:
        db.add(AppSetting(key="ai.min_refresh_interval_hours", value=0, updated_at=now))
        db.add(AppSetting(key="ai.refresh_min_new_reviews", value=1, updated_at=now))
        db.add(AppSetting(key="ai.refresh_min_growth", value=0.0, updated_at=now))
        db.commit()

    add_reviews(game.id, Audience.CRITICS, ["critic:tuned-game:one-more"])
    client = StubGeminiClient()
    outcome, _ = enrich(game.id, client)

    assert outcome.summaries["critics"] == GENERATED
    assert client.review_calls == [("Tuned Game", 5, 0)]


def test_tags_are_stored_with_their_facet_and_reused() -> None:
    game = store("tagged-game")
    client = StubGeminiClient(
        facets={"mechanics": ["action-combat", "exploration"], "mood": ["dark"]}
    )
    outcome, _ = enrich(game.id, client)

    assert outcome.tags == GENERATED
    with SessionLocal() as db:
        tags = {tag.slug: tag.facet for tag in db.scalars(select(Tag))}
        assert tags == {"action-combat": "mechanics", "exploration": "mechanics", "dark": "mood"}
        stored = db.get(Game, game.id)
        assert len(stored.tags) == 3
        assert stored.ai_tags_digest
        assert stored.ai_tags_model == "test-model"


def test_a_temporary_failure_is_recorded_and_the_next_game_still_runs() -> None:
    broken = store("broken-game")
    healthy = store("healthy-game")
    client = StubGeminiClient(review_error=GeminiTemporaryError("429 quota exceeded"))

    with SessionLocal() as db:
        session = EnrichmentSession(db, client=client)

    outcome, session = enrich(broken.id, client, session)
    assert outcome.summaries == {"critics": FAILED, "users": FAILED}
    assert "429" in outcome.error
    assert session.disabled_reason is None

    client.review_error = None
    healthy_outcome, _ = enrich(healthy.id, client, session)
    assert healthy_outcome.summaries == {"critics": GENERATED, "users": GENERATED}


def test_an_invalid_response_does_not_write_a_summary() -> None:
    game = store("garbled-game")
    client = StubGeminiClient(review_error=GeminiInvalidResponse("missing field 'summary'"))

    outcome, _ = enrich(game.id, client)

    assert outcome.summaries == {"critics": FAILED, "users": FAILED}
    assert summaries(game.id) == {}


def test_a_credential_failure_disables_gemini_for_the_rest_of_the_run() -> None:
    first = store("first-game")
    second = store("second-game")
    client = StubGeminiClient(review_error=GeminiUnavailable("401 API key not valid"))

    with SessionLocal() as db:
        session = EnrichmentSession(db, client=client)

    outcome, session = enrich(first.id, client, session)
    assert outcome.error and "401" in outcome.error
    assert session.disabled_reason
    assert not session.active

    client.review_error = None
    skipped, _ = enrich(second.id, client, session)
    assert skipped.error == session.disabled_reason
    assert client.review_calls == [("First Game", 4, 4)]  # no second attempt
    assert summaries(second.id) == {}


def test_enrichment_is_skipped_when_turned_off_in_settings() -> None:
    game = store("disabled-game")
    with SessionLocal() as db:
        db.add(AppSetting(key="ai.enabled", value=False, updated_at=utc_now()))
        db.commit()

    client = StubGeminiClient()
    outcome, session = enrich(game.id, client)

    assert not session.enabled
    assert client.review_calls == []
    assert outcome.error == "AI enrichment is turned off in settings"


def test_missing_api_key_disables_enrichment_without_touching_the_sdk() -> None:
    store("keyless-game")
    with SessionLocal() as db:
        session = EnrichmentSession(db)
    assert not session.enabled
    assert session.disabled_reason == "GEMINI_API_KEY is not configured"


@pytest.mark.parametrize("audience", [Audience.CRITICS, Audience.USERS])
def test_only_the_audience_that_needs_work_is_sent_to_the_model(audience: Audience) -> None:
    game = store("partial-game", critics=4, users=4)
    enrich(game.id, StubGeminiClient())
    now = utc_now()
    with SessionLocal() as db:
        row = db.scalar(
            select(ReviewSummary).where(
                ReviewSummary.game_id == game.id, ReviewSummary.audience == audience
            )
        )
        row.generated_at = now - timedelta(days=2)
        db.commit()
    add_reviews(game.id, audience, [f"{audience.value}:partial:{index}" for index in range(6)])

    client = StubGeminiClient()
    enrich(game.id, client)

    critics_sent, users_sent = client.review_calls[0][1:]
    if audience is Audience.CRITICS:
        assert (critics_sent, users_sent) == (10, 0)
    else:
        assert (critics_sent, users_sent) == (0, 10)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReviewSummary)) == 2


def test_a_new_prompt_version_invalidates_stored_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = store("versioned-game")
    enrich(game.id, StubGeminiClient())

    # A prompt revision must regenerate even though no review changed.
    monkeypatch.setattr(enrichment, "PROMPT_VERSION", "99")
    client = StubGeminiClient(liked=["Rewritten by the new prompt"])
    outcome, _ = enrich(game.id, client)

    assert outcome.summaries == {"critics": GENERATED, "users": GENERATED}
    assert summaries(game.id)[Audience.CRITICS].positives == ["Rewritten by the new prompt"]

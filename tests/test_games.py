from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.collectors.metacritic import PlatformScore
from app.db import SessionLocal
from app.models import (
    Audience,
    Game,
    GamePlatform,
    Platform,
    ReviewSummary,
    Tag,
    YouTubeAnalysis,
    game_tags,
)
from app.services.games import apply_game_snapshot, upsert_discovered_game
from app.time import utc_now
from tests.conftest import build_snapshot


def test_discovery_updates_existing_game() -> None:
    with SessionLocal() as db:
        first = upsert_discovered_game(
            db,
            title="Example Game",
            release_date=date(2026, 1, 2),
            external_key="example-game",
            description="First description",
        )
        first_id = first.id
        db.commit()

        second = upsert_discovered_game(
            db,
            title="Example Game: Updated",
            release_date=date(2026, 1, 2),
            external_key="example-game",
            description="Refreshed description",
        )
        db.commit()

        assert second.id == first_id
        assert second.description == "Refreshed description"
        assert db.scalar(select(func.count()).select_from(Game)) == 1


def test_games_search_platform_filter_and_sort(authenticated_client: TestClient) -> None:
    now = utc_now()
    with SessionLocal() as db:
        pc = Platform(slug="pc", name="PC", created_at=now)
        xbox = Platform(slug="xbox", name="Xbox", created_at=now)
        alpha = upsert_discovered_game(db, title="Alpha Quest", external_key="alpha")
        beta = upsert_discovered_game(db, title="Beta Drive", external_key="beta")
        db.add_all([pc, xbox])
        db.flush()
        db.add_all(
            [
                GamePlatform(
                    game=alpha,
                    platform=pc,
                    metascore=92,
                    userscore=8.7,
                    created_at=now,
                    updated_at=now,
                ),
                GamePlatform(
                    game=beta,
                    platform=xbox,
                    metascore=75,
                    userscore=7.1,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        db.commit()

    response = authenticated_client.get("/games?q=Alpha&platform=pc&sort=metascore")
    assert response.status_code == 200
    assert "Alpha Quest" in response.text
    assert "Beta Drive" not in response.text
    assert "92" in response.text


def test_game_detail_page(authenticated_client: TestClient) -> None:
    with SessionLocal() as db:
        game = upsert_discovered_game(db, title="Detail Game", external_key="detail")
        game_id = game.id
        db.commit()

    response = authenticated_client.get(f"/games/{game_id}")
    assert response.status_code == 200
    assert "Detail Game" in response.text
    assert "Platform scores" in response.text


def test_snapshot_is_stored_and_rendered_on_the_detail_page(
    authenticated_client: TestClient,
) -> None:
    snapshot = build_snapshot("cover-game", title="Cover Game", platforms=("pc", "xbox-series-x"))
    with SessionLocal() as db:
        game = apply_game_snapshot(db, snapshot)
        db.commit()
        game_id = game.id

    response = authenticated_client.get(f"/games/{game_id}")
    assert response.status_code == 200
    assert snapshot.cover_image_url in response.text
    assert snapshot.video_url in response.text
    assert "Action" in response.text
    assert "Critic opinion 0 about cover-game" in response.text
    assert "Player opinion 1 about cover-game" in response.text
    assert "XBOX-SERIES-X" in response.text


def catalogue_game(
    slug: str,
    *,
    title: str,
    platforms: list[tuple[str, int | None, str | None, int]],
    release: date | None = None,
) -> Game:
    """A game whose platforms carry different scores and review counts."""
    snapshot = build_snapshot(slug, title=title, reviews=0, platforms=())
    snapshot.release_date = release
    for name, metascore, userscore, critics in platforms:
        snapshot.platforms.append(
            PlatformScore(
                slug=name,
                name=name.upper(),
                metascore=metascore,
                userscore=Decimal(userscore) if userscore else None,
                critic_review_count=critics,
            )
        )
    with SessionLocal() as db:
        game = apply_game_snapshot(db, snapshot)
        db.commit()
        return game


def test_catalogue_shows_the_lead_platform_score_and_release_date(
    authenticated_client: TestClient,
) -> None:
    catalogue_game(
        "multi",
        title="Multi Platform Game",
        release=date(2026, 5, 4),
        platforms=[("pc", 91, "9.1", 3), ("ps5", 74, "6.2", 18)],
    )

    response = authenticated_client.get("/games")
    assert response.status_code == 200
    body = response.text
    assert "2026-05-04" in body
    # PS5 has the most critic reviews, so its scores represent the game. The assertions
    # match rendered cells, because the page also carries a random CSRF token.
    assert "<strong>74</strong>" in body
    assert "<strong>6.2</strong>" in body
    assert "<strong>91</strong>" not in body
    assert "Updated" not in body


def test_platform_badges_link_to_the_platform_filter(authenticated_client: TestClient) -> None:
    catalogue_game("badges", title="Badge Game", platforms=[("pc", 80, "8.0", 5)])

    response = authenticated_client.get("/games")
    assert "/games?platform=pc" in response.text

    filtered = authenticated_client.get("/games?platform=pc")
    assert "Badge Game" in filtered.text


def test_sorting_uses_the_lead_platform_score_and_keeps_unrated_last(
    authenticated_client: TestClient,
) -> None:
    catalogue_game(
        "high", title="High Lead", platforms=[("pc", 60, "6.0", 30), ("ps5", 95, None, 1)]
    )
    catalogue_game("mid", title="Mid Lead", platforms=[("pc", 82, "8.2", 12)])
    catalogue_game("none", title="Unrated Game", platforms=[("pc", None, None, 0)])

    body = authenticated_client.get("/games?sort=metascore").text
    order = [body.index(title) for title in ("Mid Lead", "High Lead", "Unrated Game")]
    assert order == sorted(order)
    # Nothing invents a score for the unrated game.
    assert "not rated" in body
    assert ">0<" not in body


def test_detail_page_shows_summaries_tags_and_similar_games(
    authenticated_client: TestClient,
) -> None:
    now = utc_now()
    first = catalogue_game("hero-a", title="Hero A", platforms=[("pc", 80, "8.0", 10)])
    second = catalogue_game("hero-b", title="Hero B", platforms=[("pc", 78, "7.9", 9)])
    with SessionLocal() as db:
        tag = Tag(slug="platforming", name="platforming", facet="mechanics")
        db.add(tag)
        db.flush()
        for game_id in (first.id, second.id):
            db.execute(game_tags.insert().values(game_id=game_id, tag_id=tag.id))
        db.add(
            ReviewSummary(
                game_id=first.id,
                audience=Audience.CRITICS,
                summary="Critics enjoyed the pacing.",
                verdict="positive",
                positives=["Tight level design"],
                negatives=["Short campaign"],
                source_count=7,
                model_name="test-model",
                generated_at=now,
                updated_at=now,
            )
        )
        db.commit()

    body = authenticated_client.get(f"/games/{first.id}").text
    assert "Critics enjoyed the pacing." in body
    assert "Tight level design" in body
    assert "Short campaign" in body
    assert "From 7 critic reviews" in body
    assert "No player reviews collected yet." in body
    assert "Hero B" in body
    assert f"/games/{second.id}" in body
    assert "Shared gameplay: platforming" in body


def test_detail_page_shows_youtube_source_and_speech_grounded_findings(
    authenticated_client: TestClient,
) -> None:
    game = catalogue_game("youtube", title="Video Game", platforms=[("pc", 80, "8.0", 10)])
    now = utc_now()
    with SessionLocal() as db:
        db.add(
            YouTubeAnalysis(
                game_id=game.id,
                status="success",
                video_id="abc123",
                video_url="https://www.youtube.com/watch?v=abc123",
                video_title="Video Game Let's Play Part 1",
                channel_title="Thoughtful Creator",
                view_count=1_234_567,
                duration_seconds=3600,
                fragment_start_seconds=2700,
                fragment_end_seconds=3600,
                speech_transcript="The combat is quick, but the checkpoints are frustrating.",
                summary="The creator enjoys combat but dislikes checkpoint placement.",
                liked=["Combat reacts quickly to inputs"],
                disliked=["Checkpoints repeat too much progress"],
                analysis_data={"prompt_version": "4"},
                model_name="test-video-model",
                analyzed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

    body = authenticated_client.get(f"/games/{game.id}").text
    assert "Let&#39;s-play perspective" in body or "Let's-play perspective" in body
    assert "Thoughtful Creator" in body
    assert "1,234,567 views" in body
    assert "Watch on YouTube" in body
    assert "The creator enjoys combat" in body
    assert "Combat reacts quickly" in body
    assert "Checkpoints repeat too much" in body
    assert "Speech evidence from the fragment" in body

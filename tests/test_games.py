from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Game, GamePlatform, Platform
from app.services.games import upsert_discovered_game
from app.time import utc_now


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

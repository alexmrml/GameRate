"""Client behaviour over a stubbed transport: request flow, retries, failures.

The transport replays the same page captures the parser tests use, so the whole
collect_game sequence is covered without reaching Metacritic.
"""

from decimal import Decimal

import httpx
import pytest

from app.collectors.metacritic import (
    CRITIC_AUDIENCE,
    USER_AUDIENCE,
    MetacriticClient,
    MetacriticError,
    MetacriticNotFound,
)
from tests.conftest import load_fixture

BASE = "https://metacritic.test"


def build_client(handler) -> MetacriticClient:
    transport = httpx.MockTransport(handler)
    return MetacriticClient(
        httpx.Client(transport=transport, base_url=BASE),
        base_url=BASE,
        delay_seconds=0,
        sleep=lambda _seconds: None,
    )


def metacritic_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    platform = request.url.params.get("platform")
    if path == "/game/":
        return httpx.Response(200, text=load_fixture("new_releases.html"))
    if path == "/browse/game/all/all/all-time/new/":
        page = request.url.params.get("page", "1")
        return httpx.Response(200, text=load_fixture(f"browse_page_{page}.html"))
    if path == "/game/elden-ring/":
        return httpx.Response(200, text=load_fixture("game_elden_ring.html"))
    if path == "/game/elden-ring/critic-reviews/":
        return httpx.Response(200, text=load_fixture("critic_reviews.html"))
    if path == "/game/elden-ring/user-reviews/":
        name = "user_reviews.html" if platform == "xbox-series-x" else "user_reviews_empty.html"
        return httpx.Response(200, text=load_fixture(name))
    return httpx.Response(404, text="not found")


def test_listing_endpoints_return_slugs() -> None:
    with build_client(metacritic_handler) as client:
        assert len(client.new_release_slugs()) == 20
        assert client.browse_slugs(1)[0] == "settlers-domain"
        assert client.browse_slugs(2)[0] == "furry-roommates"


def test_collect_game_merges_metascores_userscores_and_reviews() -> None:
    with build_client(metacritic_handler) as client:
        game = client.collect_game("elden-ring")

    by_slug = {platform.slug: platform for platform in game.platforms}
    assert by_slug["xbox-series-x"].metascore == 96
    assert by_slug["xbox-series-x"].userscore == Decimal(8)
    assert by_slug["xbox-series-x"].user_rating_count == 3283
    assert by_slug["pc"].metascore == 94
    assert by_slug["pc"].userscore is None

    assert game.review_count(CRITIC_AUDIENCE) == 10
    assert game.review_count(USER_AUDIENCE) == 25  # capped per platform
    assert len({review.external_key for review in game.reviews}) == len(game.reviews)


def test_missing_game_raises_instead_of_returning_an_empty_snapshot() -> None:
    with build_client(metacritic_handler) as client, pytest.raises(MetacriticNotFound):
        client.collect_game("no-such-game")


def test_temporary_failures_are_retried() -> None:
    attempts = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, text="busy")
        return metacritic_handler(request)

    with build_client(flaky) as client:
        assert len(client.new_release_slugs()) == 20
    assert attempts["count"] == 3


def test_persistent_failures_surface_as_an_error() -> None:
    client = build_client(lambda request: httpx.Response(503, text="busy"))
    with client, pytest.raises(MetacriticError, match="503"):
        client.new_release_slugs()

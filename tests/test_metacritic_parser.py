"""Parser tests against trimmed captures of real Metacritic pages.

The fixtures keep the genuine Nuxt payload, so these tests never touch the network
while still exercising the exact structure Metacritic serves.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.collectors.metacritic import (
    CRITIC_AUDIENCE,
    USER_AUDIENCE,
    parse_browse_page,
    parse_critic_reviews,
    parse_game_page,
    parse_new_releases,
    parse_user_reviews,
)
from app.collectors.nuxt import NuxtPayloadError
from tests.conftest import load_fixture


def test_new_releases_carousel_yields_the_first_twenty_games() -> None:
    slugs = parse_new_releases(load_fixture("new_releases.html"))

    assert len(slugs) == 20
    assert slugs[0] == "over-requiemz"
    assert "madden-nfl-27" in slugs
    assert len(set(slugs)) == len(slugs)


def test_browse_pages_are_distinct_and_ordered() -> None:
    first = parse_browse_page(load_fixture("browse_page_1.html"))
    second = parse_browse_page(load_fixture("browse_page_2.html"))

    assert len(first) == 24
    assert len(second) == 24
    assert first[0] == "settlers-domain"
    assert first[0] not in second
    # The listing shifts as Metacritic adds releases, so consecutive pages can repeat a
    # few games. The crawler relies on its own per-day filter rather than page identity.
    assert len(set(second) - set(first)) >= 20


def test_game_page_reads_metadata_video_and_platform_metascores() -> None:
    game = parse_game_page(load_fixture("game_elden_ring.html"))

    assert game.slug == "elden-ring"
    assert game.title == "Elden Ring"
    assert game.developer == "From Software"
    assert game.publisher == "Bandai Namco Games"
    assert game.release_date == date(2022, 2, 25)
    assert game.genres == ["Action RPG"]
    assert game.description is not None and "Miyazaki" in game.description
    assert game.cover_image_url == (
        "https://www.metacritic.com/a/img/catalog/provider/6/3/6-1-824862-13.jpg"
    )
    assert game.video_url == "https://cdn.jwplayer.com/players/OXExoEAD.html"
    assert game.esrb_rating == "M"
    # Metacritic's own genre-peer carousel, which similarity matching reads as a signal.
    assert "hades-ii" in game.related_slugs
    assert game.slug not in game.related_slugs

    scores = {platform.slug: platform.metascore for platform in game.platforms}
    assert scores == {
        "xbox-one": None,
        "pc": 94,
        "playstation-4": None,
        "playstation-5": 96,
        "xbox-series-x": 96,
    }
    pc = next(platform for platform in game.platforms if platform.slug == "pc")
    assert pc.critic_review_count == 63
    assert pc.source_url == "https://www.metacritic.com/game/elden-ring/critic-reviews/?platform=pc"


def test_multi_platform_game_keeps_per_platform_scores_apart() -> None:
    game = parse_game_page(load_fixture("game_madden_nfl_27.html"))

    by_slug = {platform.slug: platform for platform in game.platforms}
    assert by_slug["playstation-5"].metascore == 77
    assert by_slug["playstation-5"].critic_review_count == 10
    assert by_slug["xbox-series-x"].critic_review_count == 7
    assert by_slug["pc"].metascore is None
    assert game.video_url is None
    assert game.esrb_rating == "E"
    assert "madden-nfl-2003" in game.related_slugs


def test_incomplete_game_page_parses_without_inventing_data() -> None:
    game = parse_game_page(load_fixture("game_sparse.html"))

    assert game.title == "Settler's Domain"
    assert game.description is None
    assert game.video_url is None
    assert len(game.platforms) == 1
    assert game.platforms[0].metascore is None
    assert game.platforms[0].userscore is None
    assert game.cover_image_url is not None
    assert game.esrb_rating is None


def test_critic_reviews_carry_score_publication_and_platform() -> None:
    reviews = parse_critic_reviews(
        load_fixture("critic_reviews.html"), {"playstation 5": "playstation-5"}
    )

    assert len(reviews) == 10
    assert all(review.audience == CRITIC_AUDIENCE for review in reviews)
    assert all(review.quote for review in reviews)
    first = reviews[0]
    assert first.publication == "Areajugones"
    assert first.score == Decimal(100)
    assert first.platform_slug == "playstation-5"
    assert first.review_date == date(2022, 2, 23)
    assert len({review.external_key for review in reviews}) == len(reviews)


def test_user_reviews_expose_the_platform_userscore() -> None:
    userscore, rating_count, reviews = parse_user_reviews(
        load_fixture("user_reviews.html"), {"xbox series x": "xbox-series-x"}
    )

    assert userscore == Decimal(8)
    assert rating_count == 3283
    assert len(reviews) == 50
    assert all(review.audience == USER_AUDIENCE for review in reviews)
    assert reviews[0].author
    assert reviews[0].platform_slug == "xbox-series-x"


def test_user_reviews_page_without_reviews_returns_no_score() -> None:
    userscore, rating_count, reviews = parse_user_reviews(
        load_fixture("user_reviews_empty.html"), {}
    )

    assert userscore is None
    assert rating_count is None
    assert reviews == []


def test_pending_userscore_is_recorded_as_unknown_not_zero() -> None:
    # Metacritic serves a "tbd" userscore as 0 with no sentiment.
    userscore, rating_count, reviews = parse_user_reviews(load_fixture("user_reviews_tbd.html"), {})

    assert userscore is None
    assert rating_count == 0
    assert reviews == []


def test_document_without_payload_is_an_error_not_empty_data() -> None:
    with pytest.raises(NuxtPayloadError):
        parse_game_page("<html><body>Access denied</body></html>")

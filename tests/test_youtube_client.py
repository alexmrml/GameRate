"""YouTube discovery uses one search and deterministic metadata filtering."""

import json

import httpx
import pytest

from app.collectors.youtube import (
    MAX_QUERY_BRANCHES,
    VideoCandidate,
    YouTubeClient,
    YouTubeQuotaExceeded,
    build_search_query,
    candidate_rejection_reason,
    search_title,
)

GAMING = "20"


def response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload),
        headers={"content-type": "application/json"},
    )


def video(
    video_id: str,
    title: str,
    views: int,
    *,
    duration: str = "PT45M",
    description: str = "",
    category: str = GAMING,
) -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description,
            "channelId": f"channel-{video_id}",
            "channelTitle": f"Creator {video_id}",
            "publishedAt": "2025-01-02T03:04:05Z",
            "categoryId": category,
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": str(views)},
        "status": {"privacyStatus": "public", "embeddable": True},
    }


def candidate(
    title: str,
    *,
    views: int = 1000,
    duration: int = 2400,
    description: str = "",
    category: str | None = GAMING,
) -> VideoCandidate:
    return VideoCandidate(
        video_id="v",
        url="https://www.youtube.com/watch?v=v",
        title=title,
        channel_id="c",
        channel_title="Creator",
        published_at=None,
        view_count=views,
        duration_seconds=duration,
        description=description,
        category_id=category,
    )


def test_most_viewed_relevant_candidate_wins_after_filtering() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/search"):
            assert "videoDuration" not in request.url.params
            assert request.url.params["order"] == "viewCount"
            return response(
                {
                    "items": [
                        {"id": {"videoId": item}}
                        for item in ("trailer", "silent", "winner", "smaller", "short")
                    ]
                }
            )
        return response(
            {
                "items": [
                    video("trailer", "Elden Ring Official Gameplay Trailer", 50_000_000),
                    video(
                        "silent",
                        "Elden Ring Gameplay Walkthrough - No Commentary",
                        25_000_000,
                    ),
                    video(
                        "winner",
                        "Elden Ring Let's Play - Part 1",
                        12_000_000,
                        description="Elden Ring, part one of the campaign.",
                    ),
                    video("smaller", "Elden Ring Playthrough Episode 1", 3_000_000),
                    video("short", "Elden Ring Gameplay #shorts", 20_000_000, duration="PT45S"),
                    video("sketch", "So I Tried Elden Ring | EP 1", 13_000_000, duration="PT88S"),
                    video(
                        "lecture",
                        "Elden Ring in Medieval History",
                        30_000_000,
                        category="27",
                    ),
                ]
            }
        )

    http = httpx.Client(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    result = YouTubeClient(api_key="fake", client=http).search_game("Elden Ring")

    assert len(calls) == 2
    assert result.selected().video_id == "winner"
    reasons = {item.video_id: item.rejection_reason for item in result.candidates}
    assert reasons["trailer"] == "excluded:trailer"
    assert reasons["silent"] == "no_commentary"
    assert reasons["short"] == "shorts"
    assert reasons["sketch"] == "too_short"
    assert reasons["lecture"] == "not_gaming"


def test_the_query_stays_within_the_branch_count_youtube_can_serve() -> None:
    """Recall collapses on longer quoted OR-chains, which starved obscure games entirely."""
    query = build_search_query("Wild Blue Skies")

    assert query.count("|") == MAX_QUERY_BRANCHES - 1
    assert query.startswith('"Wild Blue Skies" ')
    assert query.count('"Wild Blue Skies"') == MAX_QUERY_BRANCHES


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("FEED IT (Games On A Chair)", "FEED IT"),
        (
            "The Lord of the Rings: War in the North - Legacy Edition",
            "The Lord of the Rings: War in the North",
        ),
        ("Mortal Shell II", "Mortal Shell II"),
    ],
)
def test_store_packaging_is_stripped_from_the_searched_name(title: str, expected: str) -> None:
    assert search_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Slayblade - Part 1",
        "Extremely Stylish Beyblade Roguelike! | Slayblade",
        "THEROPODS - A Dinosaur Adventure Game! | Demo Gameplay",  # a demo is a real let's-play
    ],
)
def test_small_channel_titles_are_kept(title: str) -> None:
    game = "Slayblade" if "Slayblade" in title else "Theropods"
    described = candidate(title, description=f"Playing {game} gameplay, part one.")
    assert candidate_rejection_reason(described, game) is None


def test_an_ambiguous_name_must_also_appear_in_the_description() -> None:
    """Where a short name is a level in a bigger game, the description is about that game."""
    jusant = candidate(
        "Jusant - Daymark",
        description="Jusant gameplay walkthrough, climbing the tower.",
    )

    assert candidate_rejection_reason(jusant, "Daymark") == "name_absent_from_description"


@pytest.mark.parametrize(
    ("title", "game", "reason"),
    [
        # An ordinary word as a game name matched any video that happened to contain it.
        ("JUSANT - Chapter 1 - Daymark", "Daymark", "name_not_prominent"),
        (
            "This Eldritch Entity's Shadows Will Consume Everything If I Feed It",
            "FEED IT",
            "name_not_prominent",
        ),
        ("Gallipoli from the Ottoman Perspective", "Gallipoli", "not_gaming"),
        ("Mortal Shell II Review - Before You Buy", "Mortal Shell II", "excluded:review"),
        ("Mortal Shell 2 All Bosses", "Mortal Shell II", "excluded:all_bosses"),
        ("Some Other Game Walkthrough", "Mortal Shell II", "different_game"),
    ],
)
def test_wrong_or_useless_results_stay_rejected(title: str, game: str, reason: str) -> None:
    category = "27" if reason == "not_gaming" else GAMING
    assert candidate_rejection_reason(candidate(title, category=category), game) == reason


def test_a_games_name_does_not_claim_its_sequel() -> None:
    sequel = candidate("Mortal Shell II Gameplay Walkthrough Part 1")

    assert candidate_rejection_reason(sequel, "Mortal Shell") == "different_game"
    assert candidate_rejection_reason(sequel, "Mortal Shell II") is None


def test_roman_and_arabic_spellings_of_the_same_game_both_match() -> None:
    assert (
        candidate_rejection_reason(candidate("So I Tried Mortal Shell 2.."), "Mortal Shell II")
        is None
    )


def test_a_dotted_acronym_matches_its_compact_spelling() -> None:
    stalker = candidate("STALKER 2 Heart of Chornobyl Gameplay Walkthrough")

    assert candidate_rejection_reason(stalker, "S.T.A.L.K.E.R. 2: Heart of Chornobyl") is None


def test_a_distinctive_name_needs_no_gameplay_keyword() -> None:
    """Requiring one for every game is what used to reject small channels outright."""
    assert (
        candidate_rejection_reason(candidate("Servant of the Lake #1"), "Servant of the Lake")
        is None
    )


def test_empty_search_stops_without_a_metadata_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response({"items": []})

    http = httpx.Client(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    result = YouTubeClient(api_key="fake", client=http).search_game("Obscure Game")

    assert calls == 1
    assert result.candidates == []
    assert result.selected() is None


def test_quota_error_has_its_own_exception() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response(
            {
                "error": {
                    "message": "quota exhausted",
                    "errors": [{"reason": "quotaExceeded"}],
                }
            },
            status=403,
        )

    http = httpx.Client(
        base_url="https://www.googleapis.com/youtube/v3",
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeClient(api_key="fake", client=http)

    try:
        client.search_game("Any Game")
    except YouTubeQuotaExceeded as exc:
        assert "quota" in str(exc)
    else:
        raise AssertionError("quota error was not classified")

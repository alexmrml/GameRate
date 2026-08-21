"""YouTube discovery uses one search and deterministic metadata filtering."""

import json

import httpx

from app.collectors.youtube import YouTubeClient, YouTubeQuotaExceeded


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
) -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description,
            "channelId": f"channel-{video_id}",
            "channelTitle": f"Creator {video_id}",
            "publishedAt": "2025-01-02T03:04:05Z",
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": str(views)},
        "status": {"privacyStatus": "public", "embeddable": True},
    }


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
                        description="Includes a Full Game Review and story campaign.",
                    ),
                    video("smaller", "Elden Ring Playthrough Episode 1", 3_000_000),
                    video("short", "Elden Ring Gameplay #shorts", 20_000_000, duration="PT45S"),
                    video("sketch", "So I Tried Elden Ring | EP 1", 13_000_000, duration="PT88S"),
                    video("demo", "Elden Ring Gameplay Demo", 14_000_000),
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
    assert reasons["silent"] == "excluded:no_commentary"
    assert reasons["short"] == "shorts_or_too_short"
    assert reasons["sketch"] == "shorts_or_too_short"
    assert reasons["demo"] == "excluded:gameplay_demo"


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

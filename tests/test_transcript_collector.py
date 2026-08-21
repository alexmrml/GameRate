"""Caption track choice, json3 decoding and the tail-window density scan."""

import json

import pytest

from app.collectors.transcript import (
    Cue,
    TranscriptClient,
    TranscriptTooQuiet,
    TranscriptTrack,
    TranscriptUnavailable,
    choose_caption_track,
    parse_json3,
    select_tail_window,
)


def track_url(language: str, *, translated: bool = False) -> str:
    suffix = f"&tlang={language}" if translated else ""
    return f"https://www.youtube.com/api/timedtext?lang={language}{suffix}"


def caption_store(*languages: str, translated: tuple[str, ...] = ()) -> dict:
    store = {language: [{"ext": "json3", "url": track_url(language)}] for language in languages}
    for language in translated:
        store[language] = [{"ext": "json3", "url": track_url(language, translated=True)}]
    return store


def json3(*events: tuple[int, int, str], extra: list[dict] | None = None) -> str:
    payload = [
        {"tStartMs": start * 1000, "dDurationMs": duration * 1000, "segs": [{"utf8": text}]}
        for start, duration, text in events
    ]
    return json.dumps({"events": payload + (extra or [])})


def speaking_track(
    *, duration: int, words_per_minute: int, until: int | None = None
) -> TranscriptTrack:
    """A track speaking at a steady rate up to `until`, then silent to the end."""
    end = duration if until is None else until
    cues = [
        Cue(start=float(second), end=float(second + 1), text=" ".join(["слово"] * words_per_minute))
        for second in range(0, end, 60)
    ]
    return TranscriptTrack(
        video_id="video",
        language="ru",
        is_automatic=True,
        duration_seconds=float(duration),
        cues=cues,
    )


def test_scrolling_repeats_and_sound_labels_are_not_counted_as_speech() -> None:
    payload = json.dumps(
        {
            "events": [
                {"tStartMs": 0, "dDurationMs": 2000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
                {"tStartMs": 10, "dDurationMs": 2000, "segs": [{"utf8": "this "}, {"utf8": "is"}]},
                {"tStartMs": 3000, "dDurationMs": 1000, "segs": [{"utf8": "[Music]"}]},
                {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "   "}]},
                {"tStartMs": 5000, "dDurationMs": 1000, "segs": [{"utf8": "real speech"}]},
            ]
        }
    )

    cues = parse_json3(payload)

    assert [cue.text for cue in cues] == ["this is", "real speech"]
    assert cues[1].start == 5.0


def test_manual_subtitles_win_over_automatic_captions() -> None:
    info = {
        "language": "en",
        "subtitles": caption_store("en"),
        "automatic_captions": caption_store("en"),
    }

    url, language, is_automatic = choose_caption_track(info)

    assert language == "en"
    assert is_automatic is False
    assert url == track_url("en")


def test_the_videos_own_language_beats_a_machine_translation_of_it() -> None:
    """A translated ASR track is a translation of a transcription: never valid evidence."""
    info = {
        "language": "ru",
        "subtitles": {},
        "automatic_captions": caption_store("ru", translated=("en", "de")),
    }

    url, language, is_automatic = choose_caption_track(info)

    assert (language, is_automatic) == ("ru", True)
    assert "tlang=" not in url


def test_a_video_with_only_translated_tracks_counts_as_having_no_captions() -> None:
    info = {"language": "ru", "automatic_captions": caption_store(translated=("en",))}

    with pytest.raises(TranscriptUnavailable):
        choose_caption_track(info)


def test_client_reads_captions_without_touching_any_media() -> None:
    requested: list[str] = []
    info = {
        "duration": 600,
        "language": "en",
        "automatic_captions": caption_store("en"),
    }

    client = TranscriptClient(
        extract_info=lambda url: requested.append(url) or info,
        fetch_url=lambda url: json3((0, 3, "hello there friend")),
    )
    track = client.fetch("abc123")

    assert requested == ["https://www.youtube.com/watch?v=abc123"]
    assert track.duration_seconds == 600
    assert track.language == "en"
    assert track.is_automatic is True
    assert track.word_count == 3


def test_a_talkative_ending_is_analyzed_at_the_very_end() -> None:
    track = speaking_track(duration=3600, words_per_minute=100)

    window = select_tail_window(track, window_seconds=900, min_words_per_minute=15)

    assert (window.start_seconds, window.end_seconds) == (2700, 3600)
    assert window.words_per_minute == pytest.approx(100, abs=1)


def test_a_silent_outro_pushes_the_window_back_but_keeps_it_near_the_end() -> None:
    # Twenty minutes of credits after a two-hour let's-play.
    track = speaking_track(duration=7200, words_per_minute=90, until=6000)

    window = select_tail_window(track, window_seconds=900, min_words_per_minute=15)

    assert window.end_seconds <= 7200
    assert window.words_per_minute >= 15
    # Still the tail: well past the halfway mark of the video.
    assert window.start_seconds > 3600


def test_a_video_that_is_silent_throughout_its_tail_is_rejected() -> None:
    track = speaking_track(duration=7200, words_per_minute=90, until=600)

    with pytest.raises(TranscriptTooQuiet):
        select_tail_window(track, window_seconds=900, min_words_per_minute=15)


def test_a_video_shorter_than_the_fragment_is_taken_whole() -> None:
    track = speaking_track(duration=420, words_per_minute=80)

    window = select_tail_window(track, window_seconds=900, min_words_per_minute=15)

    assert (window.start_seconds, window.end_seconds) == (0, 420)

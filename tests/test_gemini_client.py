"""Schema handling and error taxonomy of the Gemini client, without any network."""

import json
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors as genai_errors

from app.collectors.gemini import (
    FACET_VOCABULARY,
    GameContext,
    GeminiClient,
    GeminiInvalidResponse,
    GeminiTemporaryError,
    GeminiUnavailable,
    ReviewExcerpt,
    build_review_prompt,
    build_tag_prompt,
)

GAME = GameContext(
    title="Test Game",
    description="A short description",
    developer="Test Studio",
    release_year=2026,
    genres=["Action RPG"],
    platforms=["PC"],
)
CRITICS = [ReviewExcerpt(quote="The combat is stiff", publication="Site", score="70")]
USERS = [ReviewExcerpt(quote="Runs badly on my machine", author="player1", score="4")]

FULL_ANALYSIS = {
    "critics": {
        "liked": ["Level design rewards exploration"],
        "disliked": ["Combat lacks weight"],
        "verdict": "positive, with reservations",
        "summary": "Critics like the world but not the fighting.",
    },
    "users": {
        "liked": [],
        "disliked": ["Frequent stutters on PC"],
        "verdict": "mostly negative",
        "summary": "Players report performance problems.",
    },
}


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        # The last scripted outcome repeats, so retries stay easy to reason about.
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def sdk(*responses) -> SimpleNamespace:
    return SimpleNamespace(models=FakeModels(responses))


def reply(payload) -> SimpleNamespace:
    """A response the way the SDK returns it when `parsed` could not be built."""
    return SimpleNamespace(parsed=None, text=json.dumps(payload))


def client_for(fake, **kwargs) -> GeminiClient:
    return GeminiClient(client=fake, model="test-model", sleep=lambda _s: None, **kwargs)


def test_structured_reply_is_validated_into_separate_audiences() -> None:
    fake = sdk(reply(FULL_ANALYSIS))
    analysis = client_for(fake).analyze_reviews(GAME, CRITICS, USERS)

    assert analysis.critics.liked == ["Level design rewards exploration"]
    assert analysis.critics.verdict == "positive, with reservations"
    assert analysis.users.liked == []
    assert analysis.users.disliked == ["Frequent stutters on PC"]
    assert analysis.model == "test-model"


def test_only_the_requested_audience_is_part_of_the_schema() -> None:
    fake = sdk(reply({"critics": FULL_ANALYSIS["critics"]}))
    analysis = client_for(fake).analyze_reviews(GAME, CRITICS, [])

    assert analysis.users is None
    schema = fake.models.calls[0].config.response_schema
    assert set(schema.model_fields) == {"critics"}


def test_an_answer_that_breaks_the_schema_is_retried_then_reported() -> None:
    fake = sdk(reply({"critics": {"liked": ["ok"]}}))  # missing verdict and summary

    with pytest.raises(GeminiInvalidResponse):
        client_for(fake).analyze_reviews(GAME, CRITICS, [])
    assert len(fake.models.calls) == 3  # gemini_max_retries


def test_a_retried_call_can_still_succeed() -> None:
    fake = sdk(reply({"critics": {"liked": []}}), reply({"critics": FULL_ANALYSIS["critics"]}))
    analysis = client_for(fake).analyze_reviews(GAME, CRITICS, [])

    assert analysis.critics.summary == "Critics like the world but not the fighting."
    assert len(fake.models.calls) == 2


def test_retries_wait_thirty_seconds_between_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("app.collectors.gemini.time.monotonic", lambda: clock[0])
    fake = sdk(reply({"critics": {"liked": []}}))
    client = GeminiClient(client=fake, model="test-model", sleep=sleep)

    with pytest.raises(GeminiInvalidResponse):
        client.analyze_reviews(GAME, CRITICS, [])

    assert waits == [30.0, 30.0]


def test_a_single_attempt_client_does_not_sleep_or_retry() -> None:
    waits: list[float] = []
    fake = sdk(reply({"critics": {"liked": []}}))
    client = GeminiClient(
        client=fake,
        model="test-model",
        sleep=waits.append,
        max_attempts=1,
    )

    with pytest.raises(GeminiInvalidResponse):
        client.analyze_reviews(GAME, CRITICS, [])

    assert len(fake.models.calls) == 1
    assert waits == []


def test_rate_limits_are_temporary_failures() -> None:
    error = genai_errors.APIError(429, {"error": {"message": "quota exceeded"}})
    fake = sdk(error)

    with pytest.raises(GeminiTemporaryError, match="429|quota"):
        client_for(fake).analyze_reviews(GAME, CRITICS, [])


def test_a_rejected_key_is_fatal_and_not_retried() -> None:
    error = genai_errors.APIError(403, {"error": {"message": "API key not valid"}})
    fake = sdk(error, reply(FULL_ANALYSIS))

    with pytest.raises(GeminiUnavailable):
        client_for(fake).analyze_reviews(GAME, CRITICS, USERS)
    assert len(fake.models.calls) == 1


def test_every_request_carries_the_configured_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def build(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return sdk(reply(FULL_ANALYSIS))

    monkeypatch.setattr("app.collectors.gemini.genai.Client", build)
    GeminiClient(api_key="a-key", model="test-model")

    # HttpOptions.timeout is milliseconds; without it the SDK sends no deadline at all.
    assert captured["http_options"].timeout == 600_000


def test_a_request_that_times_out_is_reported_instead_of_being_tried_again() -> None:
    waits: list[float] = []
    fake = sdk(httpx.ReadTimeout("the model never answered"))
    client = GeminiClient(client=fake, model="test-model", sleep=waits.append)

    with pytest.raises(GeminiTemporaryError, match="did not answer within 600s"):
        client.analyze_reviews(GAME, CRITICS, USERS)

    # Spending the same 600 seconds twice more would only stall the run further.
    assert len(fake.models.calls) == 1
    assert waits == []


def test_an_empty_reply_is_not_treated_as_an_empty_summary() -> None:
    fake = sdk(SimpleNamespace(parsed=None, text=""))

    with pytest.raises(GeminiInvalidResponse):
        client_for(fake).analyze_reviews(GAME, CRITICS, [])


def test_tags_outside_the_vocabulary_are_dropped() -> None:
    fake = sdk(
        reply(
            {
                "mechanics": ["action-combat", "not-a-real-tag"],
                "setting": ["fantasy"],
                "style": [],
                "structure": [],
                "mood": [],
                "descriptors": ["Time Manipulation", "x"],
            }
        )
    )
    result = client_for(fake).derive_tags(GAME)

    assert result.facets["mechanics"] == ["action-combat"]
    assert result.facets["setting"] == ["fantasy"]
    assert result.facets["descriptors"] == ["time-manipulation"]
    assert "style" not in result.facets


def test_youtube_video_uses_clipping_and_a_structured_speech_schema() -> None:
    fake = sdk(
        reply(
            {
                "has_useful_commentary": True,
                "speech_transcript": "I like the combat, but loading takes too long.",
                "overall_opinion_evidence": "I like the combat, but loading takes too long",
                "overall_impression": "Positive, with reservations about loading.",
                "liked": [
                    {
                        "statement": "Combat feels responsive",
                        "speech_evidence": "I like the combat",
                    }
                ],
                "disliked": [
                    {
                        "statement": "Loading interrupts the pace",
                        "speech_evidence": "loading takes too long",
                    }
                ],
            }
        )
    )

    result = client_for(fake).analyze_youtube_video(
        game_title="Test Game",
        video_url="https://www.youtube.com/watch?v=video123",
        start_seconds=1200,
        end_seconds=2100,
    )

    assert result.has_useful_commentary
    assert result.liked == ["Combat feels responsive"]
    call = fake.models.calls[0]
    video_part = call.contents.parts[0]
    assert video_part.file_data.file_uri.endswith("video123")
    assert video_part.video_metadata.start_offset == "1200s"
    assert video_part.video_metadata.end_offset == "2100s"
    assert set(call.config.response_schema.model_fields) == {
        "has_useful_commentary",
        "speech_transcript",
        "overall_opinion_evidence",
        "overall_impression",
        "liked",
        "disliked",
    }


def test_transcript_analysis_is_a_plain_text_call_gemma_can_serve() -> None:
    """The main path must work on the cheap high-quota model, not just the video one."""
    fake = sdk(
        reply(
            {
                "has_useful_commentary": True,
                "overall_opinion_evidence": "I like the combat",
                "overall_impression": "Автору нравится боевая система.",
                "liked": [
                    {
                        "statement": "Бои ощущаются отзывчивыми",
                        "speech_evidence": "I like the combat",
                    }
                ],
                "disliked": [],
            }
        )
    )
    gemma = GeminiClient(client=fake, model="gemma-4-31b-it", sleep=lambda _s: None)

    result = gemma.analyze_letsplay_transcript(
        game_title="Test Game",
        transcript="I like the combat, but loading takes too long.",
        language="en",
        start_seconds=1200,
        end_seconds=2100,
    )

    assert result.source == "transcript"
    assert result.model == "gemma-4-31b-it"
    # The transcript is the caller's, never the model's retelling of it.
    assert result.speech_transcript == "I like the combat, but loading takes too long."
    assert result.liked == ["Бои ощущаются отзывчивыми"]
    call = fake.models.calls[0]
    assert "speech_transcript" not in call.config.response_schema.model_fields
    assert "I like the combat, but loading takes too long." in call.contents
    assert "Russian" in call.contents  # Gemma has no system role, so the rules are inlined


def test_an_empty_transcript_is_never_sent_to_the_model() -> None:
    fake = sdk(reply({}))

    with pytest.raises(ValueError, match="non-empty transcript"):
        client_for(fake).analyze_letsplay_transcript(
            game_title="Test Game",
            transcript="   ",
            language="en",
            start_seconds=0,
            end_seconds=900,
        )
    assert fake.models.calls == []


def test_gemma_is_not_used_for_speech_analysis_without_an_audio_track() -> None:
    fake = sdk(reply({}))
    gemma = GeminiClient(client=fake, model="gemma-4-31b-it", sleep=lambda _s: None)

    with pytest.raises(GeminiUnavailable, match="audio track"):
        gemma.analyze_youtube_video(
            game_title="Test Game",
            video_url="https://www.youtube.com/watch?v=video123",
            start_seconds=0,
            end_seconds=900,
        )
    assert fake.models.calls == []


def test_findings_are_requested_in_russian_while_sources_stay_untranslated() -> None:
    fake = sdk(reply(FULL_ANALYSIS))
    client_for(fake).analyze_reviews(GAME, CRITICS, USERS)

    instruction = fake.models.calls[0].config.system_instruction
    assert "Write every generated sentence in Russian" in instruction
    assert "Do not translate or restate the" in instruction
    assert "The combat is stiff" in fake.models.calls[0].contents  # excerpt kept verbatim


def test_review_prompt_labels_audiences_and_omits_the_missing_one() -> None:
    prompt = build_review_prompt(GAME, CRITICS, [])

    assert "CRITIC REVIEW EXCERPTS (1)" in prompt
    assert "PLAYER REVIEW EXCERPTS" not in prompt
    assert "the critics" in prompt
    assert "The combat is stiff" in prompt


def test_tag_prompt_lists_the_allowed_vocabulary_and_the_facts() -> None:
    prompt = build_tag_prompt(GAME)

    assert "Test Studio" in prompt
    assert "Action RPG" in prompt
    for facet in FACET_VOCABULARY:
        assert f"{facet}:" in prompt


def test_gemma_models_get_the_rules_inside_the_prompt() -> None:
    fake = sdk(reply({"critics": FULL_ANALYSIS["critics"]}))
    gemma = GeminiClient(client=fake, model="gemma-4-31b-it", sleep=lambda _s: None)
    gemma.analyze_reviews(GAME, CRITICS, [])

    call = fake.models.calls[0]
    assert call.config.system_instruction is None
    assert "You analyse video game reviews" in call.contents


def test_a_missing_key_fails_before_any_call_is_made() -> None:
    with pytest.raises(GeminiUnavailable, match="GEMINI_API_KEY"):
        GeminiClient(api_key="")


def test_trailing_junk_after_a_valid_object_is_tolerated() -> None:
    payload = json.dumps({"critics": FULL_ANALYSIS["critics"]})
    fake = sdk(SimpleNamespace(parsed=None, text=payload + '\n{"critics": {}}'))

    analysis = client_for(fake).analyze_reviews(GAME, CRITICS, [])

    assert analysis.critics.verdict == "positive, with reservations"
    assert len(fake.models.calls) == 1

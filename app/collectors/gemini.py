"""Gemini reader for review analysis and tag derivation.

Like the Metacritic collector this module is database-free: it takes plain inputs and
returns validated dataclasses. Every model reply is constrained by a response schema and
re-validated locally, so callers never parse free text.

Failures are split into three kinds, because they need different handling:

* :class:`GeminiUnavailable` — credentials, permissions or a missing model. Nothing in this
  process will succeed, so the caller stops asking for the rest of the run.
* :class:`GeminiTemporaryError` — rate limits, quota and server errors. The affected game is
  skipped; later games in the same run may still succeed.
* :class:`GeminiInvalidResponse` — the reply did not satisfy the schema after a retry.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from app.config import settings

logger = logging.getLogger("gamerate.collectors.gemini")

CRITICS = "critics"
USERS = "users"

# Bump when the review prompt or schema changes in a way that should invalidate stored
# summaries: the enrichment service treats a different version as work to redo.
PROMPT_VERSION = "2"
YOUTUBE_PROMPT_VERSION = "4"

_FATAL_STATUS = {400, 401, 403, 404}
_TEMPORARY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class GeminiError(RuntimeError):
    """Base class for Gemini failures."""


class GeminiUnavailable(GeminiError):
    """Gemini cannot be used at all: bad key, no access, unknown model."""


class GeminiTemporaryError(GeminiError):
    """Gemini failed in a way that may succeed for the next game."""


class GeminiInvalidResponse(GeminiError):
    """Gemini answered, but not in the shape the schema requires."""


class GeminiVideoUnavailable(GeminiError):
    """The public YouTube source could not be read by Gemini."""


# --- response schemas -------------------------------------------------------------


class AudienceInsight(BaseModel):
    """One audience's view. Lists stay short so the card reads as findings, not a dump."""

    liked: list[str] = Field(default_factory=list, max_length=5)
    disliked: list[str] = Field(default_factory=list, max_length=5)
    verdict: str
    summary: str


class CriticsAndUsersInsight(BaseModel):
    critics: AudienceInsight
    users: AudienceInsight


class CriticsOnlyInsight(BaseModel):
    critics: AudienceInsight


class UsersOnlyInsight(BaseModel):
    users: AudienceInsight


class TagSet(BaseModel):
    """Facets chosen for similarity matching, plus a few free descriptors."""

    mechanics: list[str] = Field(default_factory=list, max_length=6)
    setting: list[str] = Field(default_factory=list, max_length=4)
    style: list[str] = Field(default_factory=list, max_length=4)
    structure: list[str] = Field(default_factory=list, max_length=4)
    mood: list[str] = Field(default_factory=list, max_length=3)
    descriptors: list[str] = Field(default_factory=list, max_length=5)


class VideoOpinionPoint(BaseModel):
    statement: str
    speech_evidence: str


class VideoInsight(BaseModel):
    """Speech-grounded opinion from one selected YouTube fragment."""

    has_useful_commentary: bool
    speech_transcript: str
    overall_opinion_evidence: str
    overall_impression: str
    liked: list[VideoOpinionPoint] = Field(default_factory=list, max_length=5)
    disliked: list[VideoOpinionPoint] = Field(default_factory=list, max_length=5)


# Controlled vocabulary. A fixed list keeps tags comparable between games, which is what
# the weighted similarity needs; `descriptors` catches anything the facets miss.
MECHANIC_TAGS = [
    "action-combat",
    "turn-based-combat",
    "tactics",
    "shooting",
    "melee-combat",
    "stealth",
    "platforming",
    "puzzle-solving",
    "exploration",
    "open-world",
    "metroidvania",
    "roguelike",
    "deckbuilding",
    "card-battle",
    "survival",
    "crafting",
    "base-building",
    "city-building",
    "management",
    "resource-management",
    "farming",
    "life-sim",
    "dungeon-crawling",
    "looter",
    "character-progression",
    "party-based",
    "boss-fights",
    "procedural-generation",
    "racing",
    "sports",
    "fighting",
    "rhythm",
    "tower-defense",
    "point-and-click",
    "visual-novel",
    "physics-based",
    "sandbox-creation",
    "idle-clicker",
    "horror-survival",
    "vehicle-combat",
]
SETTING_TAGS = [
    "fantasy",
    "dark-fantasy",
    "science-fiction",
    "space",
    "cyberpunk",
    "post-apocalyptic",
    "horror",
    "historical",
    "medieval",
    "modern-day",
    "military",
    "western",
    "mythology",
    "anime",
    "cartoon",
    "urban",
    "nature",
    "school-life",
    "crime",
    "sports-world",
]
STYLE_TAGS = [
    "pixel-art",
    "2d",
    "3d",
    "low-poly",
    "realistic",
    "stylized",
    "hand-drawn",
    "retro",
    "first-person",
    "third-person",
    "isometric",
    "top-down",
    "side-scrolling",
    "vr",
]
STRUCTURE_TAGS = [
    "single-player",
    "story-driven",
    "campaign",
    "co-op",
    "pvp-multiplayer",
    "mmo",
    "live-service",
    "short-experience",
    "long-campaign",
    "replayable-runs",
    "level-based",
    "open-ended",
    "linear",
]
MOOD_TAGS = [
    "cozy",
    "relaxing",
    "dark",
    "grim",
    "humorous",
    "atmospheric",
    "tense",
    "emotional",
    "competitive",
    "family-friendly",
    "whimsical",
    "challenging",
]

FACET_VOCABULARY: dict[str, list[str]] = {
    "mechanics": MECHANIC_TAGS,
    "setting": SETTING_TAGS,
    "style": STYLE_TAGS,
    "structure": STRUCTURE_TAGS,
    "mood": MOOD_TAGS,
}


# --- inputs and results -----------------------------------------------------------


@dataclass(slots=True)
class ReviewExcerpt:
    quote: str
    author: str | None = None
    publication: str | None = None
    score: str | None = None
    platform: str | None = None


@dataclass(slots=True)
class GameContext:
    title: str
    description: str | None = None
    developer: str | None = None
    publisher: str | None = None
    release_year: int | None = None
    genres: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    metascore: int | None = None
    userscore: str | None = None


@dataclass(slots=True)
class AudienceResult:
    liked: list[str]
    disliked: list[str]
    verdict: str
    summary: str


@dataclass(slots=True)
class ReviewAnalysis:
    critics: AudienceResult | None
    users: AudienceResult | None
    model: str


@dataclass(slots=True)
class TagResult:
    facets: dict[str, list[str]]
    model: str

    def flat(self) -> list[tuple[str, str]]:
        return [(facet, tag) for facet, tags in self.facets.items() for tag in tags]


@dataclass(slots=True)
class YouTubeVideoResult:
    has_useful_commentary: bool
    speech_transcript: str
    overall_opinion_evidence: str
    overall_impression: str
    liked: list[str]
    disliked: list[str]
    liked_evidence: list[str]
    disliked_evidence: list[str]
    model: str
    prompt_version: str = YOUTUBE_PROMPT_VERSION


# --- prompts ----------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """\
You analyse video game reviews for a catalogue that helps people decide what to play.

Ground rules:
- Use only the review excerpts provided. Never use outside knowledge about the game, and
  never turn the game's marketing description into an opinion.
- Critics and players are separate audiences. A point may appear in a section only if that
  section's own excerpts make it. Never copy a point from one section to the other.
- Every entry names a concrete part of the game — a system, the story, performance, price,
  bugs, length, controls, art — and says what about it. Reject wording that would fit any
  game ("great gameplay", "a masterpiece", "fun and enjoyable").
- Write each entry as a statement with a verb, never as a bare label. "Combat and gunplay"
  and "Performance issues" are rejected; "Gunplay is punchy and weighty" and "Frame rate
  drops badly in the open city" are what is wanted.
- Merge what several reviewers say about the same thing into one entry instead of repeating
  it, and put the most frequently raised points first. At most five entries per list; three
  strong entries beat five weak ones.
- When opinion on a point is split, say so in the entry ("praised by most, though a few
  call it repetitive") rather than listing it as both liked and disliked.
- If a list would have nothing an excerpt actually supports, return it empty. Do not invent
  balance, and do not soften or exaggerate what reviewers wrote.
- Entries are single sentences of at most 20 words, written in plain English, no markdown.
- `verdict` is at most six words describing that audience's overall reception, e.g.
  "positive, with reservations about combat".
- `summary` is one or two sentences (at most 45 words) covering that audience's overall
  stance and the main disagreement, if any.
"""

TAG_SYSTEM_PROMPT = """\
You label video games with tags used to find similar games.

Ground rules:
- Judge only from the supplied description and metadata. If the description is missing or
  says almost nothing, return only what the metadata clearly supports, and leave the rest
  empty rather than guessing.
- Choose values only from the allowed list of each facet. Never invent a facet value.
- Pick the tags that make this game findable next to genuinely similar games: prefer the
  defining ones over every loosely applicable one.
- `descriptors` holds up to five short lowercase-hyphenated tags for defining traits that
  the facets do not cover (for example "time-manipulation", "roguelite-deckbuilder",
  "asymmetric-multiplayer"). Leave it empty if the facets already say everything.
"""

YOUTUBE_SYSTEM_PROMPT = """\
You analyse the spoken commentary in a video game let's-play fragment.

Ground rules:
- The creator's speech in the supplied fragment is the only evidence for opinions. Do not
  infer a judgment from gameplay footage, facial expressions, music, or events on screen.
- `speech_transcript` is a compact, faithful textual representation of what the creator says
  in the fragment. Preserve the source language and omit game dialogue, lyrics, and speech
  from other embedded media. Never replace speech with a description of screen events.
- `overall_impression`, `liked`, and `disliked` are written in plain English and must each be
  supported by something the creator actually says. Leave a list empty when speech does not
  support it; do not use outside knowledge about the game.
- Treat only an evaluation of the game's quality or the creator's enjoyment as an opinion.
  Instructions, route narration, build advice, item recommendations, mechanic explanations,
  and statements about what is useful are not evidence that the creator likes or dislikes the
  game. Never summarize a walkthrough's steps as the creator's overall impression.
- `overall_impression` is an evaluation, not a topic summary. It must say how the creator
  judges or feels about the game and be grounded in explicit evaluative speech (for example,
  "I love this combat" or "the game is disappointing"). "The creator explains mechanics",
  "provides a walkthrough", or "focuses on an ideal start" are forbidden here.
- `overall_opinion_evidence` is one short, verbatim quote from the creator's transcribed
  speech that explicitly supports their broad judgment of the game or experience. It must
  occur word-for-word in `speech_transcript`. Leave it empty when there is no such quote.
- Useful, powerful, necessary, recommended, and worth picking up describe strategy, not
  enjoyment. Do not turn those words into liked points. A liked/disliked point requires the
  creator to praise, enjoy, criticize, dislike, or express frustration with a game quality.
- Every liked/disliked point carries its own short verbatim `speech_evidence`. Do not return
  a point if its evidence is absent from the transcript.
- Separate momentary frustration at one failed jump, death, puzzle, opponent, or technical
  mishap from the creator's general view of the game. Treat it as an overall criticism only
  when the creator explicitly generalizes it to the game, system, or repeated experience.
- Set `has_useful_commentary` to false when there is no creator speech, speech is mostly game
  dialogue/instructions, or the creator never evaluates the game's quality or their experience.
  A single incidental preference during an otherwise instructional fragment is not an overall
  view. When false, `overall_impression`, `liked`, and `disliked` must all be empty, while
  `overall_opinion_evidence` is empty and `speech_transcript` still records creator speech.
- Each liked/disliked entry is one concrete sentence of at most 20 words. Return at most five,
  strongest first. `overall_impression` is at most 50 words. No markdown.
"""


def build_youtube_prompt(game_title: str, start_seconds: int, end_seconds: int) -> str:
    return (
        f"Game: {game_title}\n"
        f"Use only the supplied fragment ({start_seconds}s to {end_seconds}s in the source).\n"
        "Transcribe the creator's spoken commentary and derive only speech-supported opinions."
    )


def _excerpt_block(label: str, excerpts: list[ReviewExcerpt]) -> str:
    lines = []
    for index, excerpt in enumerate(excerpts, start=1):
        who = excerpt.publication or excerpt.author or "anonymous"
        details = [who]
        if excerpt.score:
            details.append(f"score {excerpt.score}")
        if excerpt.platform:
            details.append(excerpt.platform)
        quote = " ".join(excerpt.quote.split())
        lines.append(f"[{label} {index}] ({', '.join(details)}) {quote}")
    return "\n".join(lines)


def build_review_prompt(
    game: GameContext, critics: list[ReviewExcerpt], users: list[ReviewExcerpt]
) -> str:
    """Prompt for one game. Only the audiences with excerpts are described."""
    header = [f"Game: {game.title}"]
    if game.genres:
        header.append(f"Genres: {', '.join(game.genres)}")
    if game.platforms:
        header.append(f"Platforms: {', '.join(game.platforms)}")
    if game.release_year:
        header.append(f"Released: {game.release_year}")
    header.append(
        "The lines below are the complete evidence available. Scores use the reviewer's own "
        "scale: critic scores are out of 100, player scores out of 10."
    )

    blocks = ["\n".join(header)]
    if critics:
        blocks.append(
            f"CRITIC REVIEW EXCERPTS ({len(critics)}):\n{_excerpt_block('critic', critics)}"
        )
    if users:
        blocks.append(f"PLAYER REVIEW EXCERPTS ({len(users)}):\n{_excerpt_block('player', users)}")
    blocks.append(
        "Fill the schema for "
        + ("both audiences" if critics and users else ("the critics" if critics else "the players"))
        + ", using only that audience's excerpts."
    )
    return "\n\n".join(blocks)


def build_tag_prompt(game: GameContext) -> str:
    facts = [f"Title: {game.title}"]
    if game.developer:
        facts.append(f"Developer: {game.developer}")
    if game.publisher:
        facts.append(f"Publisher: {game.publisher}")
    if game.release_year:
        facts.append(f"Release year: {game.release_year}")
    if game.genres:
        facts.append(f"Metacritic genres: {', '.join(game.genres)}")
    if game.platforms:
        facts.append(f"Platforms: {', '.join(game.platforms)}")
    if game.metascore is not None:
        facts.append(f"Metascore: {game.metascore}")
    if game.userscore is not None:
        facts.append(f"Userscore: {game.userscore}")
    description = (game.description or "").strip() or "(no description available)"

    vocabulary = "\n".join(
        f"{facet}: {', '.join(values)}" for facet, values in FACET_VOCABULARY.items()
    )
    return (
        "METADATA:\n"
        + "\n".join(facts)
        + "\n\nDESCRIPTION:\n"
        + description
        + "\n\nALLOWED FACET VALUES:\n"
        + vocabulary
    )


# --- client -----------------------------------------------------------------------


class GeminiClient:
    """Small wrapper around the official SDK with schema validation and retries."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.model = model or settings.gemini_model
        key = api_key if api_key is not None else settings.gemini_api_key
        if client is None:
            if not key:
                raise GeminiUnavailable("GEMINI_API_KEY is not configured")
            client = genai.Client(api_key=key)
        self._client = client
        self._sleep = sleep
        rpm = max(settings.gemini_requests_per_minute, 1)
        self._min_interval = 60.0 / rpm
        self._last_call_at: float | None = None

    @property
    def supports_system_instruction(self) -> bool:
        # Gemma models are served without a system role.
        return not self.model.lower().startswith("gemma")

    def _config(self, system_prompt: str, schema: type[BaseModel]) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": schema,
            "temperature": settings.gemini_temperature,
        }
        if self.supports_system_instruction:
            kwargs["system_instruction"] = system_prompt
        return types.GenerateContentConfig(**kwargs)

    def _throttle(self) -> None:
        """Keep calls inside the key's per-minute allowance instead of discovering it via 429s."""
        if self._min_interval <= 0:
            return
        if self._last_call_at is not None:
            waited = time.monotonic() - self._last_call_at
            if waited < self._min_interval:
                self._sleep(self._min_interval - waited)
        self._last_call_at = time.monotonic()

    def _generate(self, prompt: str, system_prompt: str, schema: type[BaseModel]) -> BaseModel:
        contents = prompt if self.supports_system_instruction else f"{system_prompt}\n\n{prompt}"
        return self._generate_contents(contents, system_prompt, schema)

    def _generate_contents(
        self,
        contents: Any,
        system_prompt: str,
        schema: type[BaseModel],
        *,
        video_input: bool = False,
    ) -> BaseModel:
        attempts = max(settings.gemini_max_retries, 1)
        last_error: Exception | None = None
        retry_after: float | None = None

        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                response = self._client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=self._config(system_prompt, schema),
                )
            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                message = str(exc).casefold()
                if (
                    video_input
                    and status in {400, 404}
                    and any(
                        marker in message
                        for marker in ("youtube", "video", "file uri", "file_uri", "media")
                    )
                ):
                    raise GeminiVideoUnavailable(
                        f"Gemini could not read the YouTube video ({status}): {exc}"
                    ) from exc
                if status in _FATAL_STATUS:
                    raise GeminiUnavailable(f"Gemini rejected the call ({status}): {exc}") from exc
                if status in _TEMPORARY_STATUS or status is None:
                    last_error = exc
                    retry_after = _retry_delay(exc)
                else:
                    raise GeminiTemporaryError(f"Gemini call failed ({status}): {exc}") from exc
            except Exception as exc:  # transport errors and SDK surprises
                last_error = exc
                retry_after = None
            else:
                try:
                    return self._validate(response, schema)
                except GeminiInvalidResponse as exc:
                    last_error = exc
                    retry_after = None

            if attempt < attempts:
                backoff = settings.gemini_retry_delay_seconds * (2 ** (attempt - 1))
                # A quota answer states how long to wait; that beats guessing.
                self._sleep(max(backoff, retry_after or 0.0))

        if isinstance(last_error, GeminiInvalidResponse):
            raise last_error
        raise GeminiTemporaryError(f"Gemini call failed after {attempts} attempts: {last_error}")

    def _validate(self, response: Any, schema: type[BaseModel]) -> BaseModel:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        text = getattr(response, "text", None)
        if not text:
            raise GeminiInvalidResponse("Gemini returned an empty response")
        try:
            return schema.model_validate(_first_json_object(text))
        except (json.JSONDecodeError, ValueError, ValidationError) as exc:
            raise GeminiInvalidResponse(f"Gemini response did not match the schema: {exc}") from exc

    def analyze_reviews(
        self,
        game: GameContext,
        critics: list[ReviewExcerpt],
        users: list[ReviewExcerpt],
    ) -> ReviewAnalysis:
        """Summarise the audiences that actually have reviews. Never invents a missing one."""
        if not critics and not users:
            raise ValueError("analyze_reviews needs at least one audience with reviews")
        if critics and users:
            schema: type[BaseModel] = CriticsAndUsersInsight
        elif critics:
            schema = CriticsOnlyInsight
        else:
            schema = UsersOnlyInsight

        result = self._generate(
            build_review_prompt(game, critics, users), REVIEW_SYSTEM_PROMPT, schema
        )
        return ReviewAnalysis(
            critics=_audience(getattr(result, "critics", None)),
            users=_audience(getattr(result, "users", None)),
            model=self.model,
        )

    def derive_tags(self, game: GameContext) -> TagResult:
        result = self._generate(build_tag_prompt(game), TAG_SYSTEM_PROMPT, TagSet)
        facets: dict[str, list[str]] = {}
        for facet, allowed in FACET_VOCABULARY.items():
            allowed_set = set(allowed)
            values = [value for value in getattr(result, facet, []) if value in allowed_set]
            if values:
                facets[facet] = list(dict.fromkeys(values))
        descriptors = [
            normalized
            for value in result.descriptors
            if (normalized := _normalize_descriptor(value))
        ]
        if descriptors:
            facets["descriptors"] = list(dict.fromkeys(descriptors))
        return TagResult(facets=facets, model=self.model)

    def analyze_youtube_video(
        self,
        *,
        game_title: str,
        video_url: str,
        start_seconds: int,
        end_seconds: int,
    ) -> YouTubeVideoResult:
        """Analyse a clipped public YouTube URL without downloading its media."""
        if self.model.lower().startswith("gemma"):
            raise GeminiUnavailable(
                f"{self.model} is not suitable for YouTube speech analysis: this served "
                "Gemma variant does not receive the video's audio track"
            )
        if end_seconds <= start_seconds:
            raise ValueError("YouTube fragment end must be after its start")
        prompt = build_youtube_prompt(game_title, start_seconds, end_seconds)
        contents = types.Content(
            role="user",
            parts=[
                types.Part(
                    file_data=types.FileData(file_uri=video_url),
                    video_metadata=types.VideoMetadata(
                        start_offset=f"{start_seconds}s", end_offset=f"{end_seconds}s"
                    ),
                ),
                types.Part(text=prompt),
            ],
        )
        result = self._generate_contents(
            contents, YOUTUBE_SYSTEM_PROMPT, VideoInsight, video_input=True
        )
        return YouTubeVideoResult(
            has_useful_commentary=result.has_useful_commentary,
            speech_transcript=result.speech_transcript.strip(),
            overall_opinion_evidence=result.overall_opinion_evidence.strip(),
            overall_impression=result.overall_impression.strip(),
            liked=[item.statement.strip() for item in result.liked if item.statement.strip()],
            disliked=[item.statement.strip() for item in result.disliked if item.statement.strip()],
            liked_evidence=[
                item.speech_evidence.strip() for item in result.liked if item.statement.strip()
            ],
            disliked_evidence=[
                item.speech_evidence.strip() for item in result.disliked if item.statement.strip()
            ],
            model=self.model,
        )


def _first_json_object(text: str) -> Any:
    """Decode the first JSON value in the reply.

    Some models occasionally append a second object or a stray line after a perfectly good
    answer. The schema check below stays strict; only the trailing noise is forgiven.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        value, _end = json.JSONDecoder().raw_decode(stripped)
        return value


def _retry_delay(exc: Exception) -> float | None:
    """Seconds the API asked us to wait, when it says so (RetryInfo on quota errors)."""
    match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    if match:
        return float(match.group(1))
    match = re.search(r"[Pp]lease retry in (\d+(?:\.\d+)?)s", str(exc))
    return float(match.group(1)) if match else None


def _audience(insight: AudienceInsight | None) -> AudienceResult | None:
    if insight is None:
        return None
    return AudienceResult(
        liked=[item.strip() for item in insight.liked if item.strip()],
        disliked=[item.strip() for item in insight.disliked if item.strip()],
        verdict=insight.verdict.strip(),
        summary=insight.summary.strip(),
    )


def _normalize_descriptor(value: str) -> str | None:
    slug = "-".join("".join(ch if ch.isalnum() else " " for ch in value.lower()).split())
    return slug if 2 < len(slug) <= 40 else None

"""Gemini enrichment: review summaries and similarity tags.

The expensive part of a run is the model call, so the decision *not* to call is the important
logic here. An audience is summarized once, and regenerated only when its review set has
grown enough to change the answer — measured both in absolute new reviews and relative
growth — and never more often than the quiet period allows. Tags are regenerated only when
the facts they were derived from change.

Failures never escape: a game that cannot be enriched is reported and the run continues.
Credential-level failures stop further calls for the rest of the run instead of repeating a
request that cannot succeed.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.collectors.gemini import (
    PROMPT_VERSION,
    GameContext,
    GeminiClient,
    GeminiInvalidResponse,
    GeminiTemporaryError,
    GeminiUnavailable,
    ReviewExcerpt,
)
from app.config import settings
from app.models import AIEnrichmentRetry, Audience, Game, GameReview, ReviewSummary, Tag
from app.services.app_settings import get_setting
from app.services.games import normalize_title
from app.services.similarity import lead_platform
from app.time import as_utc, utc_now

logger = logging.getLogger("gamerate.enrichment")

SKIPPED_NO_REVIEWS = "no_reviews"
SKIPPED_TOO_FEW = "too_few_reviews"
SKIPPED_UNCHANGED = "unchanged"
GENERATED = "generated"
FAILED = "failed"
REVIEWS_TASK = "reviews"
TAGS_TASK = "tags"
RETRY_TASKS = {REVIEWS_TASK, TAGS_TASK}


@dataclass(slots=True)
class EnrichmentPlan:
    """Which audiences need a model call, what each audience's state is, and tag freshness."""

    wanted: dict[Audience, tuple[list[GameReview], str]]
    status: dict[Audience, str]
    tags_stale: bool

    def rows(self, audience: Audience) -> list[GameReview]:
        entry = self.wanted.get(audience)
        return entry[0] if entry else []


@dataclass(slots=True)
class GameEnrichment:
    """What happened to one game, in a form the run details can store."""

    title: str
    summaries: dict[str, str] = field(default_factory=dict)
    tags: str = SKIPPED_UNCHANGED
    error: str | None = None
    attempted_tasks: set[str] = field(default_factory=set)
    succeeded_tasks: set[str] = field(default_factory=set)
    retryable_failures: set[str] = field(default_factory=set)

    @property
    def called_model(self) -> bool:
        return self.tags == GENERATED or GENERATED in self.summaries.values()

    def as_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {"title": self.title, "summaries": self.summaries}
        if self.tags != SKIPPED_UNCHANGED:
            details["tags"] = self.tags
        if self.error:
            details["error"] = self.error
        return details


def _digest(values: list[str]) -> str:
    """Fingerprint of the reviews a summary was built from, tied to the prompt version."""
    body = hashlib.sha256("|".join(sorted(values)).encode("utf-8")).hexdigest()
    return f"{PROMPT_VERSION}:{body}"


def _prompt_version_of(digest: str | None) -> str | None:
    return digest.split(":", 1)[0] if digest and ":" in digest else None


def _excerpts(reviews: list[GameReview], limit: int) -> list[ReviewExcerpt]:
    chosen = sorted(
        reviews,
        key=lambda review: (review.review_date is not None, review.review_date, len(review.quote)),
        reverse=True,
    )[:limit]
    return [
        ReviewExcerpt(
            quote=review.quote,
            author=review.author,
            publication=review.publication,
            score=None if review.score is None else f"{review.score.normalize():f}",
            platform=review.platform.name if review.platform is not None else None,
        )
        for review in chosen
    ]


def game_context(game: Game) -> GameContext:
    lead = lead_platform(game)
    return GameContext(
        title=game.title,
        description=game.description,
        developer=game.developer,
        publisher=game.publisher,
        release_year=game.release_date.year if game.release_date else None,
        genres=[genre.name for genre in game.genres],
        platforms=[row.platform.name for row in game.platforms if row.platform is not None],
        metascore=lead.metascore if lead is not None else None,
        userscore=None if lead is None or lead.userscore is None else str(lead.userscore),
    )


def tag_digest(game: Game) -> str:
    """Tags depend on the description and the hard facts, so only those are fingerprinted."""
    parts = [
        game.title,
        (game.description or "").strip(),
        game.developer or "",
        game.publisher or "",
        str(game.release_date or ""),
        ",".join(sorted(genre.slug for genre in game.genres)),
        ",".join(sorted(row.platform.slug for row in game.platforms if row.platform)),
    ]
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()


def summary_needs_refresh(
    existing: ReviewSummary | None,
    review_count: int,
    digest: str,
    *,
    min_reviews: int,
    min_new_reviews: int,
    min_growth: float,
    quiet_hours: int,
) -> bool:
    """Decide whether an audience is worth a model call right now."""
    if review_count < min_reviews:
        return False
    if existing is None:
        return True
    if existing.input_digest == digest:
        return False
    if _prompt_version_of(existing.input_digest) != _prompt_version_of(digest):
        # The prompt changed, so the stored answer is stale no matter how the reviews moved.
        return True

    added = review_count - (existing.source_count or 0)
    if added < min_new_reviews:
        return False
    baseline = max(existing.source_count or 0, 1)
    if added / baseline < min_growth:
        return False
    if quiet_hours:
        age = utc_now() - as_utc(existing.generated_at)
        if age < timedelta(hours=quiet_hours):
            return False
    return True


def _get_or_create_tag(db: Session, facet: str, value: str) -> Tag:
    slug = normalize_title(value)
    tag = db.scalar(select(Tag).where(Tag.slug == slug))
    if tag is None:
        tag = Tag(slug=slug, name=value.replace("-", " "), facet=facet)
        db.add(tag)
        db.flush()
    elif tag.facet is None:
        tag.facet = facet
    return tag


def _store_summary(
    db: Session,
    game: Game,
    audience: Audience,
    result: Any,
    *,
    digest: str,
    source_count: int,
    model: str,
) -> None:
    now = utc_now()
    row = db.scalar(
        select(ReviewSummary).where(
            ReviewSummary.game_id == game.id,
            ReviewSummary.audience == audience,
            ReviewSummary.platform_id.is_(None),
        )
    )
    if row is None:
        row = ReviewSummary(
            game_id=game.id, audience=audience, platform_id=None, generated_at=now, updated_at=now
        )
        db.add(row)
    row.summary = result.summary
    row.verdict = result.verdict
    row.positives = result.liked
    row.negatives = result.disliked
    row.input_digest = digest
    row.source_count = source_count
    row.model_name = model
    row.generated_at = now
    row.updated_at = now


class EnrichmentSession:
    """Holds the Gemini client and the disabled state for the duration of one run."""

    def __init__(
        self,
        db: Session,
        client: GeminiClient | None = None,
        *,
        max_attempts: int | None = None,
    ) -> None:
        self.enabled = bool(get_setting(db, "ai.enabled"))
        self.model = str(get_setting(db, "ai.model"))
        self.min_reviews = int(get_setting(db, "ai.min_reviews"))
        self.max_games = int(get_setting(db, "ai.max_games_per_run"))
        self.min_new_reviews = int(get_setting(db, "ai.refresh_min_new_reviews"))
        self.min_growth = float(get_setting(db, "ai.refresh_min_growth"))
        self.quiet_hours = int(get_setting(db, "ai.min_refresh_interval_hours"))
        self.disabled_reason: str | None = None
        self.calls = 0
        self.enriched = 0
        self._client = client
        self._client_ready = client is not None
        self._max_attempts = max_attempts

        if not self.enabled:
            self.disabled_reason = "AI enrichment is turned off in settings"
        elif client is None and not settings.gemini_api_key:
            self.enabled = False
            self.disabled_reason = "GEMINI_API_KEY is not configured"

    @property
    def active(self) -> bool:
        return self.enabled and self.disabled_reason is None and self.enriched < self.max_games

    def client(self) -> GeminiClient:
        if self._client is None:
            self._client = GeminiClient(model=self.model, max_attempts=self._max_attempts)
            self._client_ready = True
        return self._client

    def _disable(self, reason: str) -> None:
        self.disabled_reason = reason
        logger.error("gemini disabled for this run: %s", reason)

    def plan(self, db: Session, game: Game) -> "EnrichmentPlan":
        """Work out which audiences and which tags need a model call for this game."""
        reviews = db.scalars(
            select(GameReview)
            .where(GameReview.game_id == game.id)
            .options(selectinload(GameReview.platform))
        ).all()
        by_audience: dict[Audience, list[GameReview]] = {Audience.CRITICS: [], Audience.USERS: []}
        for review in reviews:
            by_audience[review.audience].append(review)

        existing = {
            row.audience: row
            for row in db.scalars(
                select(ReviewSummary).where(
                    ReviewSummary.game_id == game.id, ReviewSummary.platform_id.is_(None)
                )
            )
        }

        wanted: dict[Audience, tuple[list[GameReview], str]] = {}
        status: dict[Audience, str] = {}
        for audience, rows in by_audience.items():
            digest = _digest([row.external_key for row in rows])
            if not rows:
                status[audience] = SKIPPED_NO_REVIEWS
            elif len(rows) < self.min_reviews:
                status[audience] = SKIPPED_TOO_FEW
            elif summary_needs_refresh(
                existing.get(audience),
                len(rows),
                digest,
                min_reviews=self.min_reviews,
                min_new_reviews=self.min_new_reviews,
                min_growth=self.min_growth,
                quiet_hours=self.quiet_hours,
            ):
                wanted[audience] = (rows, digest)
                status[audience] = GENERATED
            else:
                status[audience] = SKIPPED_UNCHANGED

        return EnrichmentPlan(
            wanted=wanted, status=status, tags_stale=game.ai_tags_digest != tag_digest(game)
        )

    def enrich_game(
        self,
        db: Session,
        game: Game,
        *,
        tasks: set[str] | None = None,
    ) -> GameEnrichment:
        """Enrich one game. Never raises: problems are reported on the result."""
        if tasks is not None and not tasks <= RETRY_TASKS:
            raise ValueError(f"Unknown enrichment task: {sorted(tasks - RETRY_TASKS)}")
        outcome = GameEnrichment(title=game.title)
        if not self.enabled or self.disabled_reason:
            outcome.error = self.disabled_reason
            return outcome

        plan = self.plan(db, game)
        if tasks is not None:
            if REVIEWS_TASK not in tasks:
                for audience in plan.wanted:
                    plan.status[audience] = SKIPPED_UNCHANGED
                plan.wanted = {}
            if TAGS_TASK not in tasks:
                plan.tags_stale = False
        outcome.summaries = {audience.value: state for audience, state in plan.status.items()}
        if not plan.wanted and not plan.tags_stale:
            return outcome

        try:
            client = self.client()
        except GeminiUnavailable as exc:
            self._disable(str(exc))
            outcome.error = str(exc)
            outcome.summaries = {
                audience.value: (FAILED if audience in plan.wanted else state)
                for audience, state in plan.status.items()
            }
            return outcome

        context = game_context(game)
        limit = settings.ai_max_reviews_per_audience

        if plan.wanted:
            outcome.attempted_tasks.add(REVIEWS_TASK)
            critics = _excerpts(plan.rows(Audience.CRITICS), limit)
            users = _excerpts(plan.rows(Audience.USERS), limit)
            self.calls += 1
            try:
                analysis = client.analyze_reviews(context, critics, users)
            except GeminiUnavailable as exc:
                self._disable(str(exc))
                outcome.error = str(exc)
                for audience in plan.wanted:
                    outcome.summaries[audience.value] = FAILED
                return outcome
            except (GeminiTemporaryError, GeminiInvalidResponse) as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
                outcome.retryable_failures.add(REVIEWS_TASK)
                for audience in plan.wanted:
                    outcome.summaries[audience.value] = FAILED
                logger.warning("review analysis failed for %s: %s", game.title, exc)
            else:
                outcome.succeeded_tasks.add(REVIEWS_TASK)
                for audience, result in (
                    (Audience.CRITICS, analysis.critics),
                    (Audience.USERS, analysis.users),
                ):
                    if audience not in plan.wanted or result is None:
                        outcome.summaries.setdefault(audience.value, SKIPPED_UNCHANGED)
                        continue
                    _rows, digest = plan.wanted[audience]
                    _store_summary(
                        db,
                        game,
                        audience,
                        result,
                        digest=digest,
                        source_count=len(_rows),
                        model=analysis.model,
                    )
                    outcome.summaries[audience.value] = GENERATED

        if plan.tags_stale and not outcome.error:
            outcome.attempted_tasks.add(TAGS_TASK)
            self.calls += 1
            try:
                tag_result = client.derive_tags(context)
            except GeminiUnavailable as exc:
                self._disable(str(exc))
                outcome.error = str(exc)
                outcome.tags = FAILED
            except (GeminiTemporaryError, GeminiInvalidResponse) as exc:
                outcome.tags = FAILED
                outcome.error = outcome.error or f"{type(exc).__name__}: {exc}"
                outcome.retryable_failures.add(TAGS_TASK)
                logger.warning("tag derivation failed for %s: %s", game.title, exc)
            else:
                outcome.succeeded_tasks.add(TAGS_TASK)
                tags = [_get_or_create_tag(db, facet, value) for facet, value in tag_result.flat()]
                game.tags = tags
                game.ai_tags_digest = tag_digest(game)
                game.ai_tags_model = tag_result.model
                game.ai_tags_generated_at = utc_now()
                outcome.tags = GENERATED

        if outcome.called_model:
            self.enriched += 1
        db.flush()
        return outcome


def sync_enrichment_retry_queue(
    db: Session,
    game_id: Any,
    outcome: GameEnrichment,
) -> None:
    """Persist exhausted temporary/schema failures and clear tasks that recovered."""
    for task in outcome.succeeded_tasks:
        row = db.scalar(
            select(AIEnrichmentRetry).where(
                AIEnrichmentRetry.game_id == game_id,
                AIEnrichmentRetry.task == task,
            )
        )
        if row is not None:
            db.delete(row)

    now = utc_now()
    for task in outcome.retryable_failures:
        row = db.scalar(
            select(AIEnrichmentRetry).where(
                AIEnrichmentRetry.game_id == game_id,
                AIEnrichmentRetry.task == task,
            )
        )
        if row is None:
            row = AIEnrichmentRetry(
                game_id=game_id,
                task=task,
                last_error=(outcome.error or "Gemini request failed")[:2000],
                failed_at=now,
                last_attempted_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
        else:
            row.last_error = (outcome.error or "Gemini request failed")[:2000]
            row.last_attempted_at = now
            row.updated_at = now


def remove_enrichment_retry(db: Session, row: AIEnrichmentRetry) -> None:
    """Drop queue work that no longer has stale input even without a model call."""
    db.delete(row)

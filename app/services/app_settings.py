"""Runtime settings with an environment default and a database override.

Deployment values stay environment-owned, but the knobs that decide how often the worker
talks to Gemini and YouTube can be retuned from `/settings` without restarting a container.
Secrets are deliberately absent from this map: provider keys are only read from environment.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppSetting

logger = logging.getLogger("gamerate.settings")


@dataclass(frozen=True, slots=True)
class TunableSetting:
    key: str
    kind: type
    default_attribute: str
    description: str

    def default(self) -> Any:
        return getattr(settings, self.default_attribute)


TUNABLES: tuple[TunableSetting, ...] = (
    TunableSetting(
        "ai.enabled", bool, "ai_enabled", "Run Gemini enrichment as part of processing runs"
    ),
    TunableSetting("ai.model", str, "gemini_model", "Gemini model used for enrichment"),
    TunableSetting(
        "ai.min_reviews",
        int,
        "ai_min_reviews",
        "Reviews an audience needs before it is summarized at all",
    ),
    TunableSetting(
        "ai.max_games_per_run",
        int,
        "ai_max_games_per_run",
        "Upper bound on games enriched in one run",
    ),
    TunableSetting(
        "ai.refresh_min_new_reviews",
        int,
        "ai_refresh_min_new_reviews",
        "New reviews required before an existing summary is regenerated",
    ),
    TunableSetting(
        "ai.refresh_min_growth",
        float,
        "ai_refresh_min_growth",
        "Relative growth (0.25 = +25%) required alongside the absolute threshold",
    ),
    TunableSetting(
        "ai.min_refresh_interval_hours",
        int,
        "ai_min_refresh_interval_hours",
        "Quiet period after a summary before it may be regenerated",
    ),
    TunableSetting(
        "youtube.enabled",
        bool,
        "youtube_analysis_enabled",
        "Find and analyze a YouTube let's-play for games without a useful result",
    ),
    TunableSetting(
        "youtube.model",
        str,
        "youtube_analysis_model",
        "Model that reads the subtitle fragment of a let's-play",
    ),
    TunableSetting(
        "youtube.video_fallback_model",
        str,
        "youtube_video_fallback_model",
        "Multimodal Gemini model used only when a video publishes no usable subtitles",
    ),
    TunableSetting(
        "youtube.fragment_minutes",
        int,
        "youtube_analysis_fragment_minutes",
        "Length of the analyzed fragment near the end of the video, in minutes",
    ),
    TunableSetting(
        "youtube.min_words_per_minute",
        int,
        "youtube_transcript_min_words_per_minute",
        "Speech rate a fragment must hold to count as commentary rather than silence",
    ),
    TunableSetting(
        "youtube.max_games_per_run",
        int,
        "youtube_analysis_max_games_per_run",
        "Maximum games searched or analyzed per processing run",
    ),
    TunableSetting(
        "youtube.max_searches_per_run",
        int,
        "youtube_max_searches_per_run",
        "YouTube Data API searches per run; each costs 100 of the 10 000 daily units",
    ),
    TunableSetting(
        "youtube.max_video_fallbacks_per_run",
        int,
        "youtube_max_video_fallbacks_per_run",
        "Multimodal video calls per run, for games whose sources have no subtitles",
    ),
)

TUNABLES_BY_KEY = {item.key: item for item in TUNABLES}


def _coerce(value: Any, kind: type) -> Any:
    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return kind(value)


def get_setting(db: Session, key: str) -> Any:
    """Return the database override for a tunable, or its environment default."""
    tunable = TUNABLES_BY_KEY.get(key)
    if tunable is None:
        raise KeyError(f"Unknown tunable setting {key!r}")
    row = db.get(AppSetting, key)
    if row is None or row.value is None:
        return tunable.default()
    try:
        return _coerce(row.value, tunable.kind)
    except (TypeError, ValueError):
        logger.warning("setting %s holds %r, which is not a %s", key, row.value, tunable.kind)
        return tunable.default()


def effective_settings(db: Session) -> dict[str, Any]:
    return {tunable.key: get_setting(db, tunable.key) for tunable in TUNABLES}


def describe_settings(db: Session) -> list[dict[str, Any]]:
    """Rows for the settings page: default, override and the value in force."""
    overrides = {
        row.key: row
        for row in db.scalars(select(AppSetting).where(AppSetting.key.in_(TUNABLES_BY_KEY)))
    }
    described = []
    for tunable in TUNABLES:
        row = overrides.get(tunable.key)
        described.append(
            {
                "key": tunable.key,
                "description": tunable.description,
                "default": tunable.default(),
                "override": row.value if row is not None else None,
                "effective": get_setting(db, tunable.key),
                "updated_at": row.updated_at if row is not None else None,
            }
        )
    return described

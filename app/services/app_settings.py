"""Runtime settings with an environment default and a database override.

Deployment values stay environment-owned, but the knobs that decide how often the worker
talks to Gemini and YouTube can be retuned from `/settings` without restarting a container.
Provider keys are deliberately absent from this map. The yt-dlp proxy pool is the one
explicit exception: administrators may add individual proxy URLs through a dedicated form
which never sends their raw value back to the browser.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AppSetting
from app.time import utc_now
from app.youtube_proxies import mask_proxy_url, parse_proxy_list, validate_proxy_url

logger = logging.getLogger("gamerate.settings")
YOUTUBE_PROXIES_KEY = "youtube.proxies"


@dataclass(frozen=True, slots=True)
class TunableSetting:
    key: str
    kind: type
    default_attribute: str
    label: str
    group: str
    description: str
    unit: str | None = None
    minimum: float | None = None
    step: float | None = None

    def default(self) -> Any:
        return getattr(settings, self.default_attribute)


TUNABLES: tuple[TunableSetting, ...] = (
    TunableSetting(
        "ai.enabled",
        bool,
        "ai_enabled",
        "Обогащать данные с помощью AI",
        "ai",
        "Создавать сводки по отзывам и аналитические теги после сбора данных.",
    ),
    TunableSetting(
        "ai.model",
        str,
        "gemini_model",
        "Модель для сводок",
        "ai",
        "Модель Gemini, которая анализирует отзывы и описание игры.",
    ),
    TunableSetting(
        "ai.min_reviews",
        int,
        "ai_min_reviews",
        "Минимум отзывов для сводки",
        "ai",
        "Сколько отзывов одной аудитории нужно собрать до первого анализа.",
        "отзывов",
        1,
        1,
    ),
    TunableSetting(
        "ai.max_games_per_run",
        int,
        "ai_max_games_per_run",
        "Игр с AI-анализом за запуск",
        "ai",
        "Ограничивает объём AI-обработки в одном цикле.",
        "игр",
        0,
        1,
    ),
    TunableSetting(
        "ai.refresh_min_new_reviews",
        int,
        "ai_refresh_min_new_reviews",
        "Новых отзывов для обновления",
        "ai",
        "Абсолютный порог роста выборки перед повторным созданием сводки.",
        "отзывов",
        1,
        1,
    ),
    TunableSetting(
        "ai.refresh_min_growth",
        float,
        "ai_refresh_min_growth",
        "Относительный рост выборки",
        "ai",
        "Дополнительный порог роста: 0.25 означает увеличение на 25%.",
        "доля",
        0,
        0.05,
    ),
    TunableSetting(
        "ai.min_refresh_interval_hours",
        int,
        "ai_min_refresh_interval_hours",
        "Пауза между обновлениями",
        "ai",
        "Минимальное время после создания сводки до следующего обновления.",
        "часов",
        0,
        1,
    ),
    TunableSetting(
        "youtube.enabled",
        bool,
        "youtube_analysis_enabled",
        "Анализировать летсплеи",
        "youtube",
        "Искать YouTube-летсплеи и извлекать мнение автора из финального фрагмента.",
    ),
    TunableSetting(
        "youtube.model",
        str,
        "youtube_analysis_model",
        "Модель для субтитров",
        "youtube",
        "Текстовая модель, которая читает выбранный фрагмент субтитров.",
    ),
    TunableSetting(
        "youtube.video_fallback_model",
        str,
        "youtube_video_fallback_model",
        "Резервная модель для видео",
        "youtube",
        "Мультимодальная модель для источников без пригодных субтитров.",
    ),
    TunableSetting(
        "youtube.fragment_minutes",
        int,
        "youtube_analysis_fragment_minutes",
        "Длина фрагмента",
        "youtube",
        "Продолжительность анализируемого фрагмента ближе к концу видео.",
        "минут",
        1,
        1,
    ),
    TunableSetting(
        "youtube.min_words_per_minute",
        int,
        "youtube_transcript_min_words_per_minute",
        "Минимальная плотность речи",
        "youtube",
        "Фрагменты с меньшей плотностью считаются тишиной, титрами или меню.",
        "слов/мин",
        1,
        1,
    ),
    TunableSetting(
        "youtube.max_games_per_run",
        int,
        "youtube_analysis_max_games_per_run",
        "Игр с YouTube-анализом за запуск",
        "youtube",
        "Сколько игр из очереди можно обработать за один цикл.",
        "игр",
        0,
        1,
    ),
    TunableSetting(
        "youtube.max_video_fallbacks_per_run",
        int,
        "youtube_max_video_fallbacks_per_run",
        "Резервных видеоанализов за запуск",
        "youtube",
        "Ограничение дорогих вызовов модели для видео без субтитров.",
        "вызовов",
        0,
        1,
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


def parse_setting_value(key: str, value: str) -> Any:
    """Validate a value submitted by the user-facing settings form."""
    tunable = TUNABLES_BY_KEY.get(key)
    if tunable is None:
        raise KeyError(key)
    parsed = _coerce(value, tunable.kind)
    if tunable.minimum is not None and parsed < tunable.minimum:
        raise ValueError(f"value must be at least {tunable.minimum}")
    return parsed


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
                "label": tunable.label,
                "group": tunable.group,
                "description": tunable.description,
                "kind": tunable.kind.__name__,
                "unit": tunable.unit,
                "minimum": tunable.minimum,
                "step": tunable.step,
                "default": tunable.default(),
                "override": row.value if row is not None else None,
                "effective": get_setting(db, tunable.key),
                "updated_at": row.updated_at if row is not None else None,
            }
        )
    return described


def environment_youtube_proxies() -> list[str]:
    return parse_proxy_list(settings.youtube_proxies)


def stored_youtube_proxies(db: Session) -> list[str]:
    row = db.get(AppSetting, YOUTUBE_PROXIES_KEY)
    if row is None or row.value is None:
        return []
    try:
        return parse_proxy_list(row.value)
    except (TypeError, ValueError):
        logger.warning("setting %s contains an invalid proxy list", YOUTUBE_PROXIES_KEY)
        return []


def effective_youtube_proxies(db: Session) -> list[str]:
    return list(dict.fromkeys([*environment_youtube_proxies(), *stored_youtube_proxies(db)]))


def describe_youtube_proxies(db: Session) -> list[dict[str, Any]]:
    """Return browser-safe rows; raw proxy URLs must never enter a template context."""
    environment = environment_youtube_proxies()
    described = [
        {"masked": mask_proxy_url(proxy), "source": "environment", "delete_index": None}
        for proxy in environment
    ]
    described.extend(
        {
            "masked": mask_proxy_url(proxy),
            "source": "interface",
            "delete_index": index,
        }
        for index, proxy in enumerate(stored_youtube_proxies(db))
        if proxy not in environment
    )
    return described


def add_youtube_proxy(db: Session, value: str, *, user_id: Any) -> bool:
    """Store one UI-managed proxy and return false when it already exists."""
    proxy = validate_proxy_url(value)
    if proxy in effective_youtube_proxies(db):
        return False
    proxies = stored_youtube_proxies(db)
    proxies.append(proxy)
    row = db.get(AppSetting, YOUTUBE_PROXIES_KEY)
    now = utc_now()
    if row is None:
        db.add(
            AppSetting(
                key=YOUTUBE_PROXIES_KEY,
                value=proxies,
                description="yt-dlp proxies added through the settings page",
                updated_by_id=user_id,
                updated_at=now,
            )
        )
    else:
        row.value = proxies
        row.updated_by_id = user_id
        row.updated_at = now
    db.commit()
    return True


def remove_youtube_proxy(db: Session, index: int, *, user_id: Any) -> bool:
    """Remove a UI-managed proxy by its server-side list position."""
    proxies = stored_youtube_proxies(db)
    if index < 0 or index >= len(proxies):
        return False
    proxies.pop(index)
    row = db.get(AppSetting, YOUTUBE_PROXIES_KEY)
    if row is None:
        return False
    row.value = proxies
    row.updated_by_id = user_id
    row.updated_at = utc_now()
    db.commit()
    return True

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def utc_now() -> datetime:
    return datetime.now(UTC)


def app_today(moment: datetime | None = None) -> date:
    current = moment or utc_now()
    return current.astimezone(ZoneInfo(settings.app_timezone)).date()


def as_utc(moment: datetime) -> datetime:
    """Treat a stored instant as UTC.

    PostgreSQL returns timezone-aware values, while the SQLite engine used by the
    test suite drops the offset; comparisons must work identically on both.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)

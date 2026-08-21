from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def utc_now() -> datetime:
    return datetime.now(UTC)


def app_today(moment: datetime | None = None) -> date:
    current = moment or utc_now()
    return current.astimezone(ZoneInfo(settings.app_timezone)).date()

from datetime import UTC, datetime

from app.time import app_today


def test_app_day_uses_configured_timezone() -> None:
    assert app_today(datetime(2026, 8, 20, 22, 30, tzinfo=UTC)).isoformat() == "2026-08-21"

"""Proxy validation, masking and database pool composition."""

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import AppSetting
from app.services.app_settings import effective_youtube_proxies
from app.time import utc_now
from app.youtube_proxies import (
    InvalidYouTubeProxy,
    mask_proxy_url,
    parse_proxy_list,
    redact_proxy_from_message,
    validate_proxy_url,
)


@pytest.mark.parametrize("scheme", ["http", "https", "socks4", "socks4a", "socks5", "socks5h"])
def test_supported_proxy_protocols_include_socks(scheme: str) -> None:
    assert validate_proxy_url(f"{scheme}://user:pass@127.0.0.1:1080/") == (
        f"{scheme}://user:pass@127.0.0.1:1080"
    )


@pytest.mark.parametrize(
    "value",
    ["", "ftp://host:21", "socks5://host", "http://host:bad", "http://host:80/path"],
)
def test_invalid_proxy_urls_are_rejected_without_needing_yt_dlp(value: str) -> None:
    with pytest.raises(InvalidYouTubeProxy):
        validate_proxy_url(value)


def test_proxy_lists_are_deduplicated_and_masks_disclose_no_endpoint() -> None:
    proxy = "socks5://alice:s%40cret@203.0.113.42:1080"

    assert parse_proxy_list(f"{proxy},\n{proxy}\nhttp://198.51.100.7:8080") == [
        proxy,
        "http://198.51.100.7:8080",
    ]
    assert mask_proxy_url(proxy) == "socks5://***:***@***:***"
    redacted = redact_proxy_from_message(f"failed via {proxy} for alice / s@cret", proxy)
    assert proxy not in redacted
    assert "alice" not in redacted
    assert "s@cret" not in redacted
    assert "203.0.113.42" not in redacted


def test_environment_and_web_proxies_form_one_deduplicated_pool(monkeypatch) -> None:
    environment = "socks5://env:secret@192.0.2.10:1080"
    interface = "https://ui:secret@192.0.2.11:8443"
    monkeypatch.setattr(settings, "youtube_proxies", environment)
    with SessionLocal() as db:
        db.add(
            AppSetting(
                key="youtube.proxies",
                value=[environment, interface],
                updated_at=utc_now(),
            )
        )
        db.commit()

        assert effective_youtube_proxies(db) == [environment, interface]

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AppSetting, UserSession
from app.security import token_digest
from app.templates import format_run_message
from app.time import utc_now


def test_health_and_protected_redirect(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "ok", "active_workers": 0}

    protected = client.get("/games")
    assert protected.status_code == 303
    assert protected.headers["location"].startswith("/login")


def test_login_uses_server_side_session(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/games")
    assert response.status_code == 200
    assert "Каталог игр" in response.text

    raw_token = authenticated_client.cookies[settings.session_cookie_name]
    with SessionLocal() as db:
        session = db.scalar(select(UserSession))
        assert session is not None
        assert session.token_hash == token_digest(raw_token)
        assert raw_token != session.token_hash


def test_bad_password_is_rejected(client: TestClient, user: object) -> None:
    response = client.post(
        "/login",
        data={"username": "admin", "password": "not-the-password", "next": "/games"},
    )
    assert response.status_code == 401
    assert "Неверное имя пользователя или пароль" in response.text


def test_authenticated_mutation_requires_csrf(authenticated_client: TestClient) -> None:
    response = authenticated_client.post("/activity/runs", data={"csrf_token": "wrong"})
    assert response.status_code == 403

    activity = authenticated_client.get("/activity")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', activity.text)
    assert csrf is not None
    response = authenticated_client.post("/activity/runs", data={"csrf_token": csrf.group(1)})
    assert response.status_code == 303
    assert response.headers["location"] == "/activity"


def test_provider_keys_cannot_be_saved_or_rendered_on_settings(
    authenticated_client: TestClient,
) -> None:
    page = authenticated_client.get("/settings")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None

    rejected = authenticated_client.post(
        "/settings",
        data={
            "csrf_token": csrf.group(1),
            "key": "google.cloud.api-key",
            "value": "should-never-be-stored",
        },
    )
    assert rejected.status_code == 422

    # Even a legacy/manual row is redacted from the UI.
    with SessionLocal() as db:
        db.add(
            AppSetting(
                key="GOOGLE_CLOUD_API_KEY",
                value="legacy-secret",
                updated_at=utc_now(),
            )
        )
        db.commit()
    rendered = authenticated_client.get("/settings")
    assert "legacy-secret" not in rendered.text


def test_settings_page_uses_user_facing_controls(authenticated_client: TestClient) -> None:
    page = authenticated_client.get("/settings")
    assert page.status_code == 200
    assert "Сводки и теги" in page.text
    assert "Обогащать данные с помощью AI" in page.text
    assert "Stored values" not in page.text
    assert "<code>ai.enabled</code>" not in page.text

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    saved = authenticated_client.post(
        "/settings",
        data={"csrf_token": csrf.group(1), "key": "ai.min_reviews", "value": "4"},
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings?saved=1"
    with SessionLocal() as db:
        assert db.get(AppSetting, "ai.min_reviews").value == 4


def test_proxy_settings_never_render_the_saved_endpoint_or_credentials(
    authenticated_client: TestClient,
) -> None:
    page = authenticated_client.get("/settings")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    proxy = "socks5://private-user:private-pass@203.0.113.77:1080"

    added = authenticated_client.post(
        "/settings/youtube-proxies",
        data={"csrf_token": csrf.group(1), "proxy_url": proxy},
    )
    assert added.status_code == 303
    assert added.headers["location"] == "/settings?proxy=added#youtube-proxies"

    rendered = authenticated_client.get("/settings")
    assert proxy not in rendered.text
    assert "private-user" not in rendered.text
    assert "private-pass" not in rendered.text
    assert "203.0.113.77" not in rendered.text
    assert "socks5://***:***@***:***" in rendered.text
    assert 'type="password"' in rendered.text

    removed = authenticated_client.post(
        "/settings/youtube-proxies/0/delete",
        data={"csrf_token": csrf.group(1)},
    )
    assert removed.status_code == 303
    with SessionLocal() as db:
        assert db.get(AppSetting, "youtube.proxies").value == []


def test_activity_messages_are_localized_for_display() -> None:
    assert format_run_message("Waiting for worker") == "Ожидает воркер"
    assert (
        format_run_message("Processed 20 games · AI: 8 enriched · YouTube: 3 analyzed")
        == "Обработано игр: 20 · AI: 8 обогащено · YouTube: 3 проанализировано"
    )

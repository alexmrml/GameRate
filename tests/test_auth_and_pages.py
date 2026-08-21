import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AppSetting, UserSession
from app.security import token_digest
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
    assert "Games" in response.text

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
    assert "Invalid username or password" in response.text


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

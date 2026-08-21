import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["APP_TIMEZONE"] = "Europe/Istanbul"

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.time import utc_now  # noqa: E402


@pytest.fixture(autouse=True)
def database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def user() -> User:
    now = utc_now()
    with SessionLocal() as db:
        user = User(
            username="admin",
            password_hash=hash_password("correct-horse-battery"),
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def authenticated_client(client: TestClient, user: User) -> TestClient:
    response = client.post(
        "/login",
        data={
            "username": user.username,
            "password": "correct-horse-battery",
            "next": "/games",
        },
    )
    assert response.status_code == 303
    return client

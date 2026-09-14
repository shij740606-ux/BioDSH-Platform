import os
from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient

TEST_TOKEN = "test-platform-token-long-enough"
os.environ["BIODSH_DATABASE_URL"] = "sqlite:///./test-biodsh-platform.db"
os.environ["BIODSH_PLATFORM_TOKEN"] = TEST_TOKEN
os.environ["BIODSH_JWT_SECRET"] = "test-jwt-secret-that-is-longer-than-32-characters"
os.environ["BIODSH_AUTO_CREATE_SCHEMA"] = "true"
os.environ["BIODSH_ENVIRONMENT"] = "test"
os.environ["BIODSH_LOGIN_MAX_ATTEMPTS"] = "3"
os.environ["BIODSH_LOGIN_LOCK_MINUTES"] = "1"

from biodsh_platform.db import engine  # noqa: E402
from biodsh_platform.main import create_app  # noqa: E402
from biodsh_platform.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def bootstrap_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def admin_session(client: TestClient, bootstrap_headers: dict[str, str]) -> dict:
    created = client.post(
        "/api/v1/bootstrap/admin",
        headers=bootstrap_headers,
        json={"username": "admin", "password": "correct-horse-battery", "is_admin": True},
    )
    assert created.status_code == 201
    logged_in = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery"}
    )
    assert logged_in.status_code == 200
    return logged_in.json()


@pytest.fixture
def admin_headers(admin_session: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_session['access_token']}"}


@pytest.fixture
def user_factory(
    client: TestClient, admin_headers: dict[str, str]
) -> Callable[[str], tuple[dict, dict[str, str]]]:
    def create(username: str) -> tuple[dict, dict[str, str]]:
        password = f"{username}-secure-password"
        response = client.post(
            "/api/v1/admin/users",
            headers=admin_headers,
            json={"username": username, "password": password},
        )
        assert response.status_code == 201
        user = response.json()
        login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200
        return user, {"Authorization": f"Bearer {login.json()['access_token']}"}

    return create

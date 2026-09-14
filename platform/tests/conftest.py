import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

TEST_TOKEN = "test-platform-token-long-enough"
os.environ["BIODSH_DATABASE_URL"] = "sqlite:///./test-biodsh-platform.db"
os.environ["BIODSH_PLATFORM_TOKEN"] = TEST_TOKEN
os.environ["BIODSH_AUTO_CREATE_SCHEMA"] = "true"

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
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}

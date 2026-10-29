from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from harbor.core.config import Settings
from harbor.main import create_app


@pytest.fixture()
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test")


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client

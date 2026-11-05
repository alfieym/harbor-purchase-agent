from collections.abc import Callable

import pytest
from pydantic import ValidationError

from harbor.core.config import Settings

MANAGED_ENV = (
    "HARBOR_PORT",
    "HARBOR_CORS_ORIGINS",
    "OPENAI_BASE_URL",
    "HARBOR_OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "HARBOR_OPENAI_API_KEY",
    "OPENAI_MODEL",
    "HARBOR_OPENAI_MODEL",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Settings]:
    for key in MANAGED_ENV:
        monkeypatch.delenv(key, raising=False)

    def build(**overrides: object) -> Settings:
        return Settings(_env_file=None, **overrides)

    return build


def test_defaults(clean_env: Callable[..., Settings]) -> None:
    settings = clean_env()

    assert settings.port == 8010
    assert settings.api_prefix == "/api"
    assert settings.openai_base_url == "https://api.deepseek.com"
    assert settings.openai_model == "deepseek-chat"
    assert settings.openai_api_key.get_secret_value() == ""
    assert settings.llm_configured is False


def test_port_override(clean_env: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_PORT", "9123")

    assert clean_env().port == 9123


def test_openai_aliases(
    clean_env: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "example-model")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    settings = clean_env()

    assert settings.openai_base_url == "https://example.com/v1"
    assert settings.openai_model == "example-model"
    assert settings.llm_configured is True
    assert "sk-test" not in repr(settings)


def test_cors_origins_json(
    clean_env: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARBOR_CORS_ORIGINS", '["http://example.com"]')

    assert clean_env().cors_origins == ["http://example.com"]


def test_sessions_dir_defaults_to_absolute_repo_root(clean_env: Callable[..., Settings]) -> None:
    s = clean_env()
    assert s.sessions_dir.is_absolute()
    assert s.sessions_dir.name == "sessions"


def test_dispatch_defaults(clean_env: Callable[..., Settings]) -> None:
    settings = clean_env()

    assert settings.dispatch_enabled is True
    assert settings.dispatch_max_concurrency == 3
    assert settings.dispatch_task_timeout_seconds == 90.0
    assert settings.dispatch_batch_timeout_seconds == 75.0
    assert settings.dispatch_batch_timeout_seconds < settings.turn_timeout_seconds


def test_dispatch_batch_budget_must_fit_inside_the_turn(
    clean_env: Callable[..., Settings],
) -> None:
    with pytest.raises(ValidationError):
        clean_env(dispatch_batch_timeout_seconds=130.0)

from collections.abc import Callable

import pytest

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

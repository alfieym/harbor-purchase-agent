import json

import httpx
import pytest

from harbor.core.config import Settings
from harbor.features.catalog.search.embedder import OpenAIEmbedder


def test_openai_embedder_posts_to_embeddings_and_orders_by_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == ["a", "b"]
        assert request.headers["authorization"] == "Bearer sk-test"
        assert str(request.url).endswith("/embeddings")
        # Deliberately out of order: the embedder must sort by "index".
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.2, 0.3]},
                    {"index": 0, "embedding": [0.1, 0.0]},
                ]
            },
        )

    embedder = OpenAIEmbedder(Settings(_env_file=None), transport=httpx.MockTransport(handler))

    vectors = embedder.embed(["a", "b"])

    assert vectors == [[0.1, 0.0], [0.2, 0.3]]


def test_openai_embedder_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "bad")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    embedder = OpenAIEmbedder(Settings(_env_file=None), transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed(["a"])


def test_openai_embedder_applies_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    embedder = OpenAIEmbedder(Settings(_env_file=None), timeout=0.25)

    assert embedder._timeout == 0.25

"""Text embedding for semantic search."""

from collections.abc import Sequence
from typing import Protocol

import httpx

from harbor.core.config import Settings
from harbor.features.catalog.models import Product


class Embedder(Protocol):
    """Turns texts into fixed-size vectors, one vector per input text."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def product_text(product: Product) -> str:
    """Compose the searchable text that a product's vector is built from.

    Everything a buyer might query by is folded in: title, brand, category,
    description, highlight pairs and material tags. Semantic matches against
    any of these therefore surface the product.
    """
    highlights = " ".join(f"{item['label']}:{item['detail']}" for item in product.highlights)
    parts = [
        product.title,
        product.brand,
        product.category,
        product.description,
        highlights,
        " ".join(product.material_tags),
    ]
    return " ".join(part for part in parts if part)


class OpenAIEmbedder:
    """Embed via an OpenAI-compatible ``/embeddings`` endpoint.

    Reuses ``Settings.openai_base_url`` / ``openai_api_key`` / the embedding
    model name, so no separate provider is configured. A transport may be
    injected for tests (e.g. ``httpx.MockTransport``).
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = settings.openai_base_url.rstrip("/")
        self._api_key = settings.openai_api_key.get_secret_value()
        self._model = settings.openai_embedding_model
        self._transport = transport

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": list(texts)}
        headers = {"Authorization": f"Bearer {self._api_key}"}
        with httpx.Client(transport=self._transport) as client:
            response = client.post(f"{self._base_url}/embeddings", json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        ordered = sorted(data["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]

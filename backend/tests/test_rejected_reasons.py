"""Rejected-candidate reporting for hard-constraint search (S2-2)."""

import math
from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from qdrant_client import QdrantClient

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.constraints import facts_from_product, matches, reasons_for
from harbor.features.catalog.search.pipeline import SearchPipeline
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch
from harbor.features.catalog.search.schemas import SearchResult

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"

SPEC = ProductSearchSpec(
    category="家用电器",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.5,
    excluded_material_tags=["过敏原"],
)

# Every fixture product that fails at least one hard constraint in SPEC.
# D204 (USD 100) is not here: S2-6 converts it to CNY 720 ≤ 2000, so it's a hit now.
REJECTED = {"V104", "V105", "V109", "D201", "D205"}


class FakeEmbedder:
    """Deterministic character-frequency embedder; no network, no model."""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            counts = [0.0] * self._dim
            for char in text:
                counts[ord(char) % self._dim] += 1.0
            norm = math.sqrt(sum(value * value for value in counts)) or 1.0
            vectors.append([value / norm for value in counts])
        return vectors


def _products() -> list:
    return load_products(FIXTURE)


def _search(limit: int = 20) -> SearchResult:
    products = _products()
    embedder = FakeEmbedder()
    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, embedder).recreate(products)
    return QdrantProductSearch(client, embedder).search("养猫 轻便 吸尘器", SPEC, limit=limit)


def test_search_returns_filtered_out_with_product_ids() -> None:
    result = _search()

    assert isinstance(result, SearchResult)
    assert {candidate.product_id for candidate in result.filtered_out} == REJECTED


def test_rejected_candidate_carries_its_reason_code() -> None:
    by_id = {candidate.product_id: candidate for candidate in _search().filtered_out}

    assert [reason.code for reason in by_id["V104"].reasons] == ["price_over"]


def test_one_candidate_lists_every_failed_constraint() -> None:
    by_id = {candidate.product_id: candidate for candidate in _search().filtered_out}

    assert {reason.code for reason in by_id["D205"].reasons} == {
        "weight_over",
        "category_mismatch",
    }


def test_compliant_but_unreturned_is_not_rejected() -> None:
    result = _search(limit=2)

    assert {hit.product.product_id for hit in result.hits} == {"V102", "V101"}
    assert "V107" not in {candidate.product_id for candidate in result.filtered_out}


def test_reason_counts_agree_with_filtered_out() -> None:
    result = _search()

    expected = Counter(
        reason.code for candidate in result.filtered_out for reason in candidate.reasons
    )

    assert result.reason_counts == dict(expected)


def test_reasons_are_empty_exactly_when_matches() -> None:
    for product in _products():
        facts = facts_from_product(product)

        assert (reasons_for(facts, SPEC) == []) == matches(product, SPEC)


def test_pipeline_reports_the_same_rejected_set() -> None:
    pipeline_result = SearchPipeline(FakeEmbedder()).search(
        "养猫 轻便 吸尘器", SPEC, _products(), limit=20
    )

    assert {candidate.product_id for candidate in pipeline_result.filtered_out} == REJECTED

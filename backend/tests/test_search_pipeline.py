import math
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from qdrant_client import QdrantClient

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.persistence.seed_products import load_products
from harbor.features.catalog.search.pipeline import SearchPipeline, matches
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch

FIXTURE = Path(__file__).parent / "fixtures" / "catalog-vacuum-demo.jsonl"

SPEC = ProductSearchSpec(
    category="家用电器",
    price_max_major=Decimal("2000"),
    currency="CNY",
    weight_max_kg=1.5,
    excluded_material_tags=["过敏原"],
)

EXPECTED_SURVIVORS = {"V101", "V102", "V103", "V106", "V107", "V108", "D202", "D203", "D204"}


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


def test_filter_drops_each_failing_constraint() -> None:
    products = {p.product_id: p for p in _products()}

    # 价格超预算：4000 > 2000
    assert matches(products["V104"], SPEC) is False
    # 重量超阈值：6.0 > 1.5
    assert matches(products["V105"], SPEC) is False
    # 类目不符：母婴宠物 ≠ 家用电器
    assert matches(products["D201"], SPEC) is False
    # 币种不同但换算后预算内：USD 100 → CNY 720 ≤ 2000 → 通过（S2-6）
    assert matches(products["D204"], SPEC) is True
    # 材质排除：material_tags 含「过敏原」
    assert matches(products["V109"], SPEC) is False


def test_boundary_values_are_inclusive() -> None:
    products = {p.product_id: p for p in _products()}

    # 价格恰好 2000、重量恰好 1.5 都应通过（lte 语义）
    assert matches(products["V106"], SPEC) is True
    assert matches(products["V107"], SPEC) is True
    # 多 SKU 时按最低价判断：2500 / 999 → 999 通过
    assert matches(products["V108"], SPEC) is True


def test_five_stages_in_order() -> None:
    products = _products()
    pipeline = SearchPipeline(FakeEmbedder())

    # 1. 打分：所有候选都参与，无人被过滤
    scored = pipeline.score("养猫 轻便 吸尘器", products)
    assert len(scored) == len(products) == 14

    # 2. 过滤：只剩满足全部硬约束的
    filtered = pipeline.filter(SPEC, scored)
    assert {p.product_id for p, _ in filtered} == EXPECTED_SURVIVORS
    assert len(filtered) == 9

    # 3. 排序：分数非递增
    ordered = pipeline.sort(filtered)
    scores = [score for _, score in ordered]
    assert scores == sorted(scores, reverse=True)

    # 4. 截断：只留 top limit
    truncated = pipeline.truncate(ordered, limit=5)
    assert len(truncated) == 5
    assert truncated == ordered[:5]

    # 5. 组装：结果带 product 与 score
    hits = pipeline.assemble(truncated)
    assert len(hits) == 5
    assert all(hit.product is not None for hit in hits)
    assert all(isinstance(hit.score, float) for hit in hits)


def test_pipeline_agrees_with_qdrant() -> None:
    products = _products()
    embedder = FakeEmbedder()

    pipeline = SearchPipeline(embedder)
    pipeline_result = pipeline.search("养猫 轻便 吸尘器", SPEC, products, limit=20)
    pipeline_ids = {hit.product.product_id for hit in pipeline_result.hits}

    client = QdrantClient(":memory:")
    QdrantProductIndexer(client, embedder).recreate(products)
    qdrant = QdrantProductSearch(client, embedder)
    qdrant_result = qdrant.search("养猫 轻便 吸尘器", SPEC, limit=20)
    qdrant_ids = {hit.product.product_id for hit in qdrant_result.hits}

    assert pipeline_ids == EXPECTED_SURVIVORS
    assert qdrant_ids == EXPECTED_SURVIVORS

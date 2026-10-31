import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from qdrant_client import QdrantClient

from harbor.features.catalog.models import Money, Product, ProductSearchSpec, Sku
from harbor.features.catalog.search.qdrant_indexer import QdrantProductIndexer
from harbor.features.catalog.search.qdrant_search import QdrantProductSearch


class FakeEmbedder:
    """Deterministic local embedder for tests; no network, no model."""

    def __init__(self, dim: int = 8) -> None:
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


def _product(
    product_id: str,
    category: str,
    price_major: float,
    weight_kg: float,
    material_tags: tuple[str, ...] = (),
) -> Product:
    return Product(
        product_id=product_id,
        canonical_product_id=f"CAN-{product_id}",
        external_product_id=f"ext-{product_id}",
        title=f"{product_id} {category} 商品",
        brand="TestBrand",
        category=category,
        tax_category=category,
        description=f"{category} 描述",
        highlights=[],
        material_tags=list(material_tags),
        origin_country="CN",
        ships_to=["CN"],
        source_platform="test",
        dimensions_cm={"length": 10, "width": 10, "height": 10},
        weight_kg=weight_kg,
        rating_summary={"average": 4.0, "review_count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-S1",
                spec="标准",
                price=Money.from_major_units(price_major, "CNY"),
                stock=10,
            )
        ],
        updated_at=date(2026, 8, 1),
    )


def test_search_returns_only_products_matching_hard_constraints() -> None:
    client = QdrantClient(":memory:")
    embedder = FakeEmbedder()
    indexer = QdrantProductIndexer(client, embedder)
    search = QdrantProductSearch(client, embedder)

    indexer.recreate(
        [
            _product("P1", "母婴宠物", 500, 0.5),  # 满足全部硬约束
            _product("P2", "母婴宠物", 1500, 0.8),  # 满足全部硬约束
            _product("P3", "母婴宠物", 2500, 0.5),  # 价格超
            _product("P4", "母婴宠物", 500, 1.5),  # 重量超
            _product("P5", "数码配件", 500, 0.5),  # 类目不符
        ]
    )

    spec = ProductSearchSpec(
        category="母婴宠物",
        price_max_major=Decimal("2000"),
        currency="CNY",
        weight_max_kg=1.0,
    )
    hits = search.search("养猫 宠物 轻便", spec, limit=20).hits

    assert {hit.product.product_id for hit in hits} == {"P1", "P2"}
    for hit in hits:
        assert hit.product.category == "母婴宠物"
        assert hit.product.weight_kg <= 1.0
        assert min(sku.price.amount for sku in hit.product.skus) <= Decimal("2000")


def test_search_excludes_material_tags() -> None:
    client = QdrantClient(":memory:")
    embedder = FakeEmbedder()
    indexer = QdrantProductIndexer(client, embedder)
    search = QdrantProductSearch(client, embedder)

    indexer.recreate(
        [
            _product("P1", "母婴宠物", 500, 0.5, material_tags=("天然纤维",)),
            _product("P2", "母婴宠物", 500, 0.5, material_tags=("合成聚合物",)),
        ]
    )

    spec = ProductSearchSpec(category="母婴宠物", excluded_material_tags=["合成聚合物"])
    hits = search.search("宠物", spec, limit=20).hits

    assert {hit.product.product_id for hit in hits} == {"P1"}


def test_search_scores_are_present() -> None:
    client = QdrantClient(":memory:")
    embedder = FakeEmbedder()
    indexer = QdrantProductIndexer(client, embedder)
    search = QdrantProductSearch(client, embedder)

    indexer.recreate([_product("P1", "母婴宠物", 500, 0.5)])

    hits = search.search("养猫", ProductSearchSpec(category="母婴宠物"), limit=10).hits

    assert len(hits) == 1
    assert hits[0].product.product_id == "P1"
    assert isinstance(hits[0].score, float)

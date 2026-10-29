"""In-memory implementations of catalog repository ports."""

from collections.abc import Iterable, Sequence

from harbor.features.catalog.models import Product
from harbor.features.catalog.repo import ProductRepository


class InMemoryProductRepository(ProductRepository):
    """Pure in-memory ``ProductRepository`` for tests and the early MVP."""

    def __init__(self, products: Iterable[Product] = ()) -> None:
        self._products_by_id: dict[str, Product] = {}
        for product in products:
            if product.product_id in self._products_by_id:
                raise ValueError(f"duplicate product_id: {product.product_id}")
            self._products_by_id[product.product_id] = product

    def find_by_id(self, product_id: str) -> Product | None:
        return self._products_by_id.get(product_id)

    def find_by_ids(self, product_ids: Sequence[str]) -> list[Product]:
        return [
            product
            for product_id in product_ids
            if (product := self._products_by_id.get(product_id)) is not None
        ]

    def list_all(self) -> list[Product]:
        return list(self._products_by_id.values())

"""Catalog repository port."""

from collections.abc import Sequence
from typing import Protocol

from harbor.features.catalog.models import Product


class ProductRepository(Protocol):
    """Read-only port for product lookup."""

    def find_by_id(self, product_id: str) -> Product | None:
        """Return the product with ``product_id``, or ``None`` if it is absent."""
        ...

    def find_by_ids(self, product_ids: Sequence[str]) -> list[Product]:
        """Return found products in input order, skipping unknown IDs."""
        ...

    def list_all(self) -> list[Product]:
        """Return all products in the repository."""
        ...

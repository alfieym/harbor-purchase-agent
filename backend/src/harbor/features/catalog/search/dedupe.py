"""Canonical-id dedup for search results (S2-4).

One representative per ``canonical_product_id``, chosen by the lowest price after
converting to the spec's target currency (ties broken by score, then product_id).
The data is now clean — every multi-product canonical group is the same product,
see ``docs/catalog-data-schema.md`` — so grouping by the id alone is safe.
"""

from collections.abc import Sequence
from decimal import Decimal

from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.constraints import facts_from_product
from harbor.features.catalog.search.fx import convert


def converted_price(product: Product, spec: ProductSearchSpec) -> Decimal:
    """The product's lowest SKU price, converted to the spec's target currency."""
    facts = facts_from_product(product)
    return convert(facts.min_price_major, facts.currency, spec.currency)


def dedupe_by_canonical(
    scored: Sequence[tuple[Product, float]], spec: ProductSearchSpec
) -> list[tuple[Product, float]]:
    """Collapse ``scored`` to one entry per canonical id, the lowest-priced one.

    ``scored`` must already be sorted by score descending. The result keeps that
    order, so each group ranks by its best-scoring member, while the returned
    representative is the lowest-priced member (converted to ``spec.currency``).
    """
    representatives: dict[str, tuple[Product, float]] = {}
    for product, score in scored:
        canonical = product.canonical_product_id
        current = representatives.get(canonical)
        if current is None:
            representatives[canonical] = (product, score)
            continue
        current_product, current_score = current
        candidate = (converted_price(product, spec), -score, product.product_id)
        incumbent = (
            converted_price(current_product, spec),
            -current_score,
            current_product.product_id,
        )
        if candidate < incumbent:
            representatives[canonical] = (product, score)
    return list(representatives.values())

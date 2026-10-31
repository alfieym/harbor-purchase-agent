"""Fixed FX rates (CNY per unit) and conversion helpers.

The single source of truth for "how much is this amount worth in another
currency". Amounts are major units. The pivot currency is CNY: every rate is
expressed as CNY per 1 unit of the foreign currency, so converting between any
two currencies goes through CNY.

The table is a placeholder for the MVP — swap it for a real rate provider later.
It must stay in sync between indexing (``qdrant_indexer`` precomputes
``min_price_cny``) and querying (``constraints`` / ``qdrant_search`` convert the
budget), or the two paths will disagree on the same input.
"""

from decimal import Decimal

CNY_PER_UNIT: dict[str, Decimal] = {
    "CNY": Decimal("1"),
    "USD": Decimal("7.2"),
    "EUR": Decimal("7.8"),
    "JPY": Decimal("0.048"),
    "SGD": Decimal("5.3"),
}


def _rate(currency: str) -> Decimal:
    try:
        return CNY_PER_UNIT[currency]
    except KeyError as exc:
        raise ValueError(f"unknown currency: {currency!r}") from exc


def to_cny(amount: Decimal, currency: str) -> Decimal:
    """Convert ``amount`` in ``currency`` to CNY major units."""
    return amount * _rate(currency)


def convert(amount: Decimal, from_currency: str, to_currency: str) -> Decimal:
    """Convert ``amount`` between two currencies using CNY as the pivot."""
    return amount * _rate(from_currency) / _rate(to_currency)

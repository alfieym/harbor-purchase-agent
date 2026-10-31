"""Product search filter specification."""

from decimal import Decimal

from pydantic import BaseModel, Field

from .money import CurrencyCode


class ProductSearchSpec(BaseModel):
    """Filter for product search; this is not an API schema."""

    normalized_query: str = ""
    price_max_major: Decimal | None = Field(default=None, ge=0)
    currency: CurrencyCode = "CNY"
    category: str | None = None
    weight_max_kg: float | None = Field(default=None, ge=0)
    excluded_material_tags: list[str] = Field(default_factory=list)

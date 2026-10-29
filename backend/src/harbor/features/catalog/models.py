"""Catalog domain models: money, SKUs, products and search filters."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ISO-4217-shaped three-letter code, upper case. Membership in the official
# list is not checked, so future currencies do not require a code change.
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

# ISO-4217 minor-unit exponents for currencies in the catalog. Unknown
# currencies default to two fractional digits in ``amount_in_minor_units``.
_MINOR_UNIT_EXPONENTS: dict[str, int] = {
    "CNY": 2,
    "USD": 2,
    "EUR": 2,
    "JPY": 0,
    "SGD": 2,
}


class Money(BaseModel):
    """An amount in major currency units (e.g. 199.99 CNY, not cents)."""

    model_config = ConfigDict(frozen=True)

    amount: Decimal = Field(ge=0, description="Amount in major currency units.")
    currency: CurrencyCode = Field(description="ISO-4217 code, upper case.")

    @classmethod
    def from_major_units(
        cls,
        amount: Decimal | int | float | str,
        currency: CurrencyCode,
    ) -> "Money":
        """Build a value from an amount in major units."""
        return cls(amount=Decimal(str(amount)), currency=currency)

    @property
    def amount_in_minor_units(self) -> int:
        """Return the amount expressed in the currency's minor units."""
        exponent = _MINOR_UNIT_EXPONENTS.get(self.currency, 2)
        minor = self.amount.scaleb(exponent)
        if minor != minor.to_integral_value():
            raise ValueError(
                f"{self.amount} {self.currency} has more than {exponent} fractional digits"
            )
        return int(minor)


class Sku(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    sku_id: str
    spec: str
    price: Money
    stock: int = Field(ge=0)

    def deduct_stock(self, quantity: int) -> None:
        """Remove ``quantity`` units, rejecting insufficient stock."""
        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        if quantity > self.stock:
            raise ValueError(f"insufficient stock: requested {quantity}, available {self.stock}")
        self.stock -= quantity

    def restock(self, quantity: int) -> None:
        """Add ``quantity`` units back to stock."""
        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        self.stock += quantity


class Product(BaseModel):
    """A catalog product; ``canonical_product_id`` groups variants but is not unique."""

    # Raw records carry evaluation/localization metadata; drop it deliberately.
    model_config = ConfigDict(extra="ignore")

    product_id: str
    canonical_product_id: str
    external_product_id: str
    title: str
    brand: str
    category: str
    tax_category: str
    description: str
    highlights: list[dict[str, str]]
    material_tags: list[str]
    origin_country: str
    ships_to: list[str]
    source_platform: str
    dimensions_cm: dict[str, int]
    weight_kg: float = Field(gt=0)
    rating_summary: dict[str, float | int]
    skus: list[Sku]
    updated_at: date

    def model_post_init(self, __context: Any) -> None:
        """Enforce the product invariant after field validation."""
        if not self.skus:
            raise ValueError("Product must contain at least one Sku")


class ProductSearchSpec(BaseModel):
    """Filter for product search; this is not an API schema."""

    normalized_query: str = ""
    price_max_major: Decimal | None = Field(default=None, ge=0)
    currency: CurrencyCode = "CNY"
    excluded_material_tags: list[str] = Field(default_factory=list)

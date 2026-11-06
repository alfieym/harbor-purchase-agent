"""Money value object and currency helpers."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

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


def minor_unit_exponent(currency: str) -> int:
    """Fractional digits for ``currency``; unknown codes behave like CNY/USD."""
    return _MINOR_UNIT_EXPONENTS.get(currency, 2)


def quantize_major(amount: Decimal, currency: str) -> Decimal:
    """Round ``amount`` to the currency's minor unit using ``ROUND_HALF_UP``.

    Order pricing quantizes each unit price once, then multiplies by quantity;
    re-quantizing a line subtotal would double-round and break the invariant
    that an order total equals the sum of its lines.
    """
    quantum = Decimal(1).scaleb(-minor_unit_exponent(currency))
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


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
        exponent = minor_unit_exponent(self.currency)
        minor = self.amount.scaleb(exponent)
        if minor != minor.to_integral_value():
            raise ValueError(
                f"{self.amount} {self.currency} has more than {exponent} fractional digits"
            )
        return int(minor)

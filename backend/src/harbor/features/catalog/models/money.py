"""Money value object and currency helpers."""

from decimal import Decimal
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

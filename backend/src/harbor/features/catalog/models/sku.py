"""SKU domain model."""

from pydantic import BaseModel, ConfigDict, Field

from .money import Money


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

"""Product aggregate root."""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .sku import Sku


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

"""Frozen golden-set schema for S12 offline evaluation.

A ``GoldenCase`` is one query plus the labels derived from catalog facts. The
``spec`` is the hard-constraint input the retriever receives; ``expected_empty``
marks the two "should return nothing" buckets and keeps them out of positive-
case relevance averages.
"""

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from harbor.features.catalog.models import ProductSearchSpec
from harbor.features.catalog.models.money import CurrencyCode

Split = Literal["dev", "test"]


class EvalSeed(BaseModel):
    """A human seed: a failure mode plus the intent-to-catalog mapping.

    ``concept_terms`` are AND-ed against a product's searchable text and are
    what makes the label derivable from catalog facts instead of from a
    reviewer's memory. Leave them empty only for ``expected_empty`` seeds.
    """

    seed_id: str
    bucket: str
    category: str | None = None
    concept_terms: list[str] = Field(default_factory=list)
    queries: list[str] = Field(min_length=1)
    price_max_major: Decimal | None = None
    currency: CurrencyCode = "CNY"
    weight_max_kg: float | None = None
    excluded_material_tags: list[str] = Field(default_factory=list)
    destination_country: str | None = None
    expected_empty: bool = False
    expected_empty_reason: str | None = None
    notes: str = ""


class GoldenCase(BaseModel):
    """One frozen query with its derived labels and split assignment."""

    case_id: str
    seed_id: str
    bucket: str
    split: Split
    query: str
    spec: ProductSearchSpec
    destination_country: str | None = None
    expected_empty: bool = False
    expected_empty_reason: str | None = None
    relevant_canonicals: list[str] = Field(default_factory=list)
    concept_terms: list[str] = Field(default_factory=list)
    notes: str = ""


class GoldenManifest(BaseModel):
    """Everything needed to tell whether two runs are comparable."""

    version: str
    generator_version: int
    catalog_sha256: str
    cases_sha256: str
    seed_count: int
    case_count: int
    split_counts: dict[str, int]
    bucket_counts: dict[str, int]
    bucket_split_counts: dict[str, dict[str, int]]
    config: dict[str, Any] = Field(default_factory=dict)


class GoldenSet(BaseModel):
    """The frozen artifact: cases plus the manifest that pins them."""

    version: str
    generator_version: int
    catalog_sha256: str
    cases: list[GoldenCase]
    manifest: GoldenManifest

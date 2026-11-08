"""Golden-set generation: derivation, self-checks, split, freeze.

The catalog is synthetic here so each failure mode is pinned without relying
on the 3700-row fixture. The real-catalog test lives in
``test_eval_seeds_real_catalog.py``.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from harbor.eval.golden import (
    GoldenSetError,
    build_golden_set,
    freeze,
    load_golden,
    save_golden,
    verify_golden,
)
from harbor.eval.schema import EvalSeed
from harbor.features.catalog.models import Money, Product, Sku


def product(
    product_id,
    canonical,
    title,
    *,
    category="户外运动",
    price="100",
    currency="CNY",
    weight=0.5,
    materials=("金属",),
    ships=("CN",),
):
    return Product(
        product_id=product_id,
        canonical_product_id=canonical,
        external_product_id=product_id,
        title=title,
        brand="Acme",
        category=category,
        tax_category="general",
        description="",
        highlights=[],
        material_tags=list(materials),
        origin_country="CN",
        ships_to=list(ships),
        source_platform="shop",
        dimensions_cm={"length": 1, "width": 1, "height": 1},
        weight_kg=weight,
        rating_summary={"score": 4.5, "count": 10},
        skus=[
            Sku(
                sku_id=f"{product_id}-s",
                spec="std",
                price=Money.from_major_units(price, currency),
                stock=5,
            )
        ],
        updated_at=date(2026, 1, 1),
    )


def seed(seed_id="s1", bucket="budget_boundary", **overrides):
    data = {
        "seed_id": seed_id,
        "bucket": bucket,
        "category": "户外运动",
        "concept_terms": ["Tent"],
        "queries": ["a tent"],
    }
    data.update(overrides)
    return EvalSeed(**data)


CATALOG = [
    product("p1", "C1", "Acme Tent", price="100", materials=("金属",)),
    product("p2", "C1", "Acme Tent Pro", price="120", materials=("金属",)),
    product("p3", "C2", "Acme Tent Lite", price="300", materials=("天然纤维",)),
    product("p4", "C3", "Acme Stove", price="100"),
]


def test_derive_marks_every_matching_canonical_once():
    golden = build_golden_set(
        CATALOG,
        [seed(concept_terms=["Tent"])],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    case = golden.cases[0]
    # C2 is over the default budget only if a budget is set; no budget here.
    assert case.relevant_canonicals == ["C1", "C2"]
    assert case.split == "dev"


def test_derive_requires_every_concept_term():
    golden = build_golden_set(
        CATALOG,
        [seed(concept_terms=["Tent", "Pro"])],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    assert golden.cases[0].relevant_canonicals == ["C1"]


def test_derive_applies_hard_constraints():
    golden = build_golden_set(
        CATALOG,
        [seed(concept_terms=["Tent"], price_max_major=Decimal("200"))],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    assert golden.cases[0].relevant_canonicals == ["C1"]


def test_build_rejects_a_positive_seed_with_no_relevant_canonical():
    with pytest.raises(GoldenSetError) as excinfo:
        build_golden_set(
            CATALOG,
            [seed(concept_terms=["NoSuchProduct"])],
            catalog_sha256="catalog-1",
            bucket_quotas={},
        )
    assert any("no relevant canonical" in error for error in excinfo.value.errors)


def test_build_rejects_expected_empty_seed_that_still_has_matches():
    with pytest.raises(GoldenSetError) as excinfo:
        build_golden_set(
            CATALOG,
            [seed(expected_empty=True, price_max_major=Decimal("1000"))],
            catalog_sha256="catalog-1",
            bucket_quotas={},
        )
    assert any("expected empty but" in error for error in excinfo.value.errors)


def test_build_accepts_expected_empty_seed_with_zero_matches():
    golden = build_golden_set(
        CATALOG,
        [
            seed(
                bucket="constraint_empty",
                concept_terms=["Tent"],
                expected_empty=True,
                price_max_major=Decimal("1"),
            )
        ],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    assert golden.cases[0].relevant_canonicals == []
    assert golden.cases[0].expected_empty is True


def test_build_rejects_a_category_that_does_not_exist_unless_it_is_the_point():
    with pytest.raises(GoldenSetError) as excinfo:
        build_golden_set(
            CATALOG,
            [seed(category="汽车用品", concept_terms=[])],
            catalog_sha256="catalog-1",
            bucket_quotas={},
        )
    assert any("category does not exist" in error for error in excinfo.value.errors)

    golden = build_golden_set(
        CATALOG,
        [
            seed(
                bucket="category_missing",
                category="汽车用品",
                concept_terms=[],
                expected_empty=True,
                queries=["车载吸尘器"],
            )
        ],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    assert golden.cases[0].expected_empty


def test_all_queries_of_a_seed_stay_in_one_split():
    golden = build_golden_set(
        CATALOG,
        [seed(queries=["a tent", "tent for two"])],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    assert len(golden.cases) == 2
    assert {case.split for case in golden.cases} == {"dev"}


def test_overlapping_seeds_never_put_a_canonical_in_both_splits():
    # A and B both touch C1; the builder must keep them together.
    golden = build_golden_set(
        CATALOG,
        [
            seed("a", concept_terms=["Tent"]),
            seed("b", concept_terms=["Tent", "Lite"]),
            seed("c", concept_terms=["Stove"]),
        ],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    placement = {}
    for case in golden.cases:
        for canonical in case.relevant_canonicals:
            placement.setdefault(canonical, set()).add(case.split)
    assert all(len(splits) == 1 for splits in placement.values())
    assert {case.split for case in golden.cases if case.seed_id in {"a", "b"}} in (
        {"dev"},
        {"test"},
    )


def test_build_assigns_about_thirty_percent_of_cases_to_test():
    seeds = [seed(f"s{i}", concept_terms=[f"Term{i}"], bucket="semantic_vague") for i in range(10)]
    catalog = [product(f"p{i}", f"C{i}", f"Acme Term{i}") for i in range(10)]
    golden = build_golden_set(catalog, seeds, catalog_sha256="catalog-1", bucket_quotas={})
    counts = golden.manifest.split_counts
    assert counts == {"dev": 7, "test": 3}


def test_build_enforces_bucket_quotas():
    with pytest.raises(GoldenSetError) as excinfo:
        build_golden_set(
            CATALOG,
            [seed()],
            catalog_sha256="catalog-1",
            bucket_quotas={"semantic_vague": 1},
        )
    assert any("bucket quota" in error for error in excinfo.value.errors)


def test_build_is_deterministic():
    kwargs = dict(catalog_sha256="catalog-1", bucket_quotas={})
    seeds = [seed("a", concept_terms=["Tent"]), seed("b", concept_terms=["Stove"])]
    first = build_golden_set(CATALOG, seeds, **kwargs)
    second = build_golden_set(CATALOG, seeds, **kwargs)
    assert first.model_dump_json() == second.model_dump_json()


def test_manifest_records_counts_and_hashes():
    golden = build_golden_set(
        CATALOG,
        [
            seed("a", concept_terms=["Tent"], queries=["tent", "two tents"]),
            seed("b", bucket="semantic_vague", concept_terms=["Stove"]),
        ],
        catalog_sha256="catalog-1",
        bucket_quotas={},
    )
    manifest = golden.manifest
    assert manifest.catalog_sha256 == "catalog-1"
    assert manifest.case_count == 3
    assert manifest.seed_count == 2
    assert manifest.bucket_counts == {"budget_boundary": 2, "semantic_vague": 1}
    assert manifest.cases_sha256 == verify_golden(golden)


def test_freeze_records_a_hash_that_verification_recomputes():
    golden = freeze(
        build_golden_set(CATALOG, [seed()], catalog_sha256="catalog-1", bucket_quotas={})
    )
    assert verify_golden(golden) == golden.manifest.cases_sha256


def test_verify_detects_a_tampered_case():
    golden = freeze(
        build_golden_set(CATALOG, [seed()], catalog_sha256="catalog-1", bucket_quotas={})
    )
    golden.cases[0].query = "tampered"
    with pytest.raises(GoldenSetError):
        verify_golden(golden)


def test_save_and_load_round_trip(tmp_path):
    golden = freeze(
        build_golden_set(CATALOG, [seed()], catalog_sha256="catalog-1", bucket_quotas={})
    )
    path = tmp_path / "golden.json"
    save_golden(golden, path)
    loaded = load_golden(path)
    # Compare parsed models: the file is sorted for diffing, so raw JSON key
    # order differs even though the content is identical.
    assert loaded.model_dump() == golden.model_dump()
    assert verify_golden(loaded) == golden.manifest.cases_sha256


def test_save_writes_stable_sorted_json(tmp_path):
    golden = freeze(
        build_golden_set(CATALOG, [seed()], catalog_sha256="catalog-1", bucket_quotas={})
    )
    path = tmp_path / "golden.json"
    save_golden(golden, path)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert parsed["manifest"]["case_count"] == 1

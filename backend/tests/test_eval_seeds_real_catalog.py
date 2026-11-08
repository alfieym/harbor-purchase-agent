"""The real catalog must satisfy the seed self-checks, and the committed
artifact must equal what the generator produces.

This is the "no hand-edited golden file" guard: if the catalog changes, the
seed labels change, and this test fails until the artifact is rebuilt.
"""

import hashlib
from pathlib import Path

import pytest

from harbor.eval.golden import build_golden_set, load_golden, verify_golden
from harbor.eval.seeds import BUCKET_QUOTAS, SEEDS
from harbor.features.catalog.persistence.seed_products import load_products

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "catalog-v1.jsonl"
GOLDEN = ROOT / "data" / "eval" / "s12-golden-v1.json"


def catalog_sha256() -> str:
    return hashlib.sha256(DATA.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def built():
    return build_golden_set(
        load_products(DATA),
        SEEDS,
        catalog_sha256=catalog_sha256(),
        bucket_quotas=BUCKET_QUOTAS,
    )


def test_real_seeds_pass_every_self_check(built):
    manifest = built.manifest
    assert manifest.seed_count == len(SEEDS)
    assert manifest.case_count == sum(len(seed.queries) for seed in SEEDS)
    for bucket, minimum in BUCKET_QUOTAS.items():
        assert manifest.bucket_counts.get(bucket, 0) >= minimum


def test_every_bucket_appears_in_both_splits(built):
    for bucket, splits in built.manifest.bucket_split_counts.items():
        assert splits.get("dev", 0) > 0, bucket
        assert splits.get("test", 0) > 0, bucket


def test_expected_empty_cases_carry_no_labels(built):
    empty = [case for case in built.cases if case.expected_empty]
    assert len(empty) == 5
    assert all(case.relevant_canonicals == [] for case in empty)


def test_positive_cases_carry_labels(built):
    positive = [case for case in built.cases if not case.expected_empty]
    assert positive
    assert all(case.relevant_canonicals for case in positive)


def test_no_canonical_leaks_across_the_split(built):
    placement: dict[str, set[str]] = {}
    for case in built.cases:
        for canonical in case.relevant_canonicals:
            placement.setdefault(canonical, set()).add(case.split)
    leaked = [canonical for canonical, splits in placement.items() if len(splits) > 1]
    assert leaked == []


def test_frozen_artifact_is_committed_and_matches_the_generator(built):
    assert GOLDEN.exists(), f"missing frozen golden set: {GOLDEN}"
    frozen = load_golden(GOLDEN)
    verify_golden(frozen)
    assert frozen.model_dump() == built.model_dump()

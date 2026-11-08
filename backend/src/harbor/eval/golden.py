"""Build, freeze, save and verify the S12 golden set.

The pipeline is the one the evaluation narrative promises:

    human seeds (failure modes) → catalog-derived labels → bucket quotas +
    canonical-isolated split → self-checks → frozen file

Labels are never typed by hand. A seed names an intent and the catalog terms
that identify it; every product whose text matches **all** terms and whose
facts satisfy the hard constraints contributes its ``canonical_product_id``.
That makes the labels recomputable, and makes a stale golden file detectable
from its hash.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from harbor.eval.schema import EvalSeed, GoldenCase, GoldenManifest, GoldenSet, Split
from harbor.features.catalog.models import Product, ProductSearchSpec
from harbor.features.catalog.search.constraints import matches

__all__ = [
    "GENERATOR_VERSION",
    "SPLIT_TARGET",
    "GoldenSetError",
    "build_golden_set",
    "derive_relevant",
    "freeze",
    "load_golden",
    "save_golden",
    "verify_golden",
]

GENERATOR_VERSION = 1
SPLIT_TARGET = 0.3
K = 5
LIMIT = 10


class GoldenSetError(Exception):
    """All self-check failures, raised together so one run fixes them all."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("golden set failed self-checks:\n- " + "\n- ".join(self.errors))


def searchable_text(product: Product) -> str:
    """Everything the concept terms are matched against.

    This mirrors ``search.embedder.product_text`` in spirit — the same fields
    a semantic search sees — but deliberately includes the brand and category
    so a seed can pin a product line without relying on a description.
    """
    parts = [
        product.title,
        product.brand,
        product.category,
        product.description,
        *product.material_tags,
        *(
            f"{highlight.get('label', '')}:{highlight.get('detail', '')}"
            for highlight in product.highlights
        ),
    ]
    return "\n".join(parts).casefold()


def seed_spec(seed: EvalSeed, query: str) -> ProductSearchSpec:
    """The hard-constraint input for one query of a seed."""
    return ProductSearchSpec(
        normalized_query=query,
        price_max_major=seed.price_max_major,
        currency=seed.currency,
        category=seed.category,
        weight_max_kg=seed.weight_max_kg,
        excluded_material_tags=list(seed.excluded_material_tags),
    )


def derive_relevant(
    products: Sequence[Product], seed: EvalSeed, spec: ProductSearchSpec
) -> list[str]:
    """Sorted canonical ids whose products match every concept term and the spec.

    A canonical is relevant when **any** of its members matches; the 27
    localized/currency variants of one product are one label, not 27.
    """
    terms = [term.casefold() for term in seed.concept_terms]
    relevant: set[str] = set()
    for product in products:
        if seed.category is not None and product.category != seed.category:
            continue
        text = searchable_text(product)
        if terms and not all(term in text for term in terms):
            continue
        if matches(product, spec):
            relevant.add(product.canonical_product_id)
    return sorted(relevant)


def build_golden_set(
    products: Sequence[Product],
    seeds: Sequence[EvalSeed],
    *,
    version: str = "s12-golden-v1",
    catalog_sha256: str,
    bucket_quotas: Mapping[str, int] | None = None,
) -> GoldenSet:
    """Derive, split, self-check and freeze a golden set.

    ``bucket_quotas`` is a hard minimum per bucket. The real seed module passes
    its own table; tests pass a small one or ``{}``.
    """
    products = list(products)
    seeds = list(seeds)
    quotas = dict(bucket_quotas or {})
    errors: list[str] = []

    _check_seed_identity(seeds, errors)
    categories = {product.category for product in products}

    labels: dict[str, list[str]] = {}
    specs: dict[str, ProductSearchSpec] = {}
    for seed in seeds:
        spec = seed_spec(seed, seed.queries[0])
        specs[seed.seed_id] = spec
        if seed.expected_empty:
            satisfying = [product for product in products if matches(product, spec)]
            if satisfying:
                names = ", ".join(sorted({product.product_id for product in satisfying})[:5])
                errors.append(
                    f"seed {seed.seed_id}: expected empty but {len(satisfying)} product(s) "
                    f"satisfy the spec ({names})"
                )
            labels[seed.seed_id] = []
            continue

        if seed.category is not None and seed.category not in categories:
            errors.append(f"seed {seed.seed_id}: category does not exist: {seed.category!r}")
        if not seed.concept_terms:
            errors.append(f"seed {seed.seed_id}: a positive seed needs concept_terms")
            labels[seed.seed_id] = []
            continue
        relevant = derive_relevant(products, seed, spec)
        if not relevant:
            errors.append(
                f"seed {seed.seed_id}: no relevant canonical for terms {seed.concept_terms}"
            )
        labels[seed.seed_id] = relevant

    _check_bucket_quotas(seeds, quotas, errors)

    if errors:
        raise GoldenSetError(errors)

    splits, component_of = _assign_splits(seeds, labels)
    cases = _build_cases(seeds, labels, specs, splits)
    _check_cases(cases, component_of, errors)
    if errors:
        raise GoldenSetError(errors)

    manifest = _manifest(version, catalog_sha256, seeds, cases, quotas)
    golden = GoldenSet(
        version=version,
        generator_version=GENERATOR_VERSION,
        catalog_sha256=catalog_sha256,
        cases=cases,
        manifest=manifest,
    )
    return freeze(golden)


def freeze(golden: GoldenSet) -> GoldenSet:
    """Return a copy whose manifest carries the hash of its cases."""
    digest = _cases_sha256(golden.cases)
    manifest = golden.manifest.model_copy(update={"cases_sha256": digest})
    return golden.model_copy(update={"manifest": manifest})


def verify_golden(golden: GoldenSet) -> str:
    """Recompute the case hash and refuse a file that was edited after freezing."""
    digest = _cases_sha256(golden.cases)
    if digest != golden.manifest.cases_sha256:
        raise GoldenSetError(
            [
                "cases hash mismatch: the golden file changed after it was frozen "
                f"(manifest={golden.manifest.cases_sha256}, computed={digest})"
            ]
        )
    if golden.manifest.case_count != len(golden.cases):
        raise GoldenSetError(
            [f"manifest says {golden.manifest.case_count} cases, file has {len(golden.cases)}"]
        )
    return digest


def save_golden(golden: GoldenSet, path: str | Path) -> Path:
    """Write the frozen artifact as stable, sorted, human-diffable JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = golden.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def load_golden(path: str | Path) -> GoldenSet:
    return GoldenSet.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _check_seed_identity(seeds: Sequence[EvalSeed], errors: list[str]) -> None:
    seen: set[str] = set()
    for seed in seeds:
        if seed.seed_id in seen:
            errors.append(f"duplicate seed_id: {seed.seed_id}")
        seen.add(seed.seed_id)
        if not seed.seed_id.strip():
            errors.append("empty seed_id")
        if any(not query.strip() for query in seed.queries):
            errors.append(f"seed {seed.seed_id}: empty query")


def _check_bucket_quotas(
    seeds: Sequence[EvalSeed], quotas: Mapping[str, int], errors: list[str]
) -> None:
    counts = Counter(seed.bucket for seed in seeds)
    for bucket, minimum in quotas.items():
        if counts.get(bucket, 0) < minimum:
            errors.append(
                f"bucket quota not met for {bucket!r}: {counts.get(bucket, 0)} < {minimum}"
            )


def _assign_splits(
    seeds: Sequence[EvalSeed], labels: Mapping[str, list[str]]
) -> tuple[dict[str, Split], dict[str, int]]:
    """Assign whole seeds to dev/test, never splitting a canonical's label set.

    Seeds that share a canonical are unioned first: putting one in dev and the
    other in test would leak the same product across the split boundary.
    """
    parent = list(range(len(seeds)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    owner: dict[str, int] = {}
    for index, seed in enumerate(seeds):
        for canonical in labels[seed.seed_id]:
            if canonical in owner:
                union(index, owner[canonical])
            else:
                owner[canonical] = index

    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(seeds)):
        components[find(index)].append(index)

    def case_count(members: Sequence[int]) -> int:
        return sum(len(seeds[index].queries) for index in members)

    def key(members: Sequence[int]) -> tuple[int, str]:
        return (case_count(members), min(seeds[index].seed_id for index in members))

    ordered = sorted(components.values(), key=key)
    total_cases = sum(case_count(members) for members in ordered)
    target = max(1, round(total_cases * SPLIT_TARGET)) if total_cases >= 2 else 0

    component_ids = [tuple(members) for members in ordered]
    component_cases = {
        identity: case_count(members)
        for identity, members in zip(component_ids, ordered, strict=True)
    }
    component_buckets = {
        identity: {seeds[index].bucket for index in members}
        for identity, members in zip(component_ids, ordered, strict=True)
    }
    bucket_components: dict[str, set[tuple[int, ...]]] = defaultdict(set)
    for identity in component_ids:
        for bucket in component_buckets[identity]:
            bucket_components[bucket].add(identity)

    split_of_component: dict[tuple[int, ...], Split] = {
        identity: "dev" for identity in component_ids
    }
    test_cases = 0

    def ranking(identity: tuple[int, ...]) -> tuple[int, str]:
        return (
            component_cases[identity],
            min(seeds[index].seed_id for index in identity),
        )

    def can_move(identity: tuple[int, ...]) -> bool:
        # Never take the last dev component of a bucket that spans several
        # components: that bucket must stay representable in both splits.
        for bucket in component_buckets[identity]:
            if len(bucket_components[bucket]) < 2:
                continue
            if not any(
                split_of_component[other] == "dev"
                for other in bucket_components[bucket]
                if other != identity
            ):
                return False
        return True

    # 1. Structural requirement first: every bucket with at least two
    #    components gets one test component (and keeps at least one dev).
    for bucket in sorted(bucket_components):
        if len(bucket_components[bucket]) < 2:
            continue
        if any(split_of_component[identity] == "test" for identity in bucket_components[bucket]):
            continue
        for identity in sorted(bucket_components[bucket], key=ranking):
            if can_move(identity):
                split_of_component[identity] = "test"
                test_cases += component_cases[identity]
                break

    # 2. Fill up to the ~30% target with the smallest remaining components.
    for identity in sorted(component_ids, key=ranking):
        if test_cases >= target:
            break
        if split_of_component[identity] == "test":
            continue
        if test_cases + component_cases[identity] > target:
            continue
        if can_move(identity):
            split_of_component[identity] = "test"
            test_cases += component_cases[identity]

    result: dict[str, Split] = {}
    component_of: dict[str, int] = {}
    for members in ordered:
        identity = tuple(members)
        split = split_of_component[identity]
        for index in members:
            result[seeds[index].seed_id] = split
            component_of[seeds[index].seed_id] = find(index)
    return result, component_of


def _build_cases(
    seeds: Sequence[EvalSeed],
    labels: Mapping[str, list[str]],
    specs: Mapping[str, ProductSearchSpec],
    splits: Mapping[str, Split],
) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    for seed in seeds:
        for index, query in enumerate(seed.queries, start=1):
            spec = specs[seed.seed_id].model_copy(update={"normalized_query": query})
            cases.append(
                GoldenCase(
                    case_id=f"{seed.seed_id}-q{index}",
                    seed_id=seed.seed_id,
                    bucket=seed.bucket,
                    split=splits[seed.seed_id],
                    query=query,
                    spec=spec,
                    destination_country=seed.destination_country,
                    expected_empty=seed.expected_empty,
                    expected_empty_reason=seed.expected_empty_reason,
                    relevant_canonicals=list(labels[seed.seed_id]),
                    concept_terms=list(seed.concept_terms),
                    notes=seed.notes,
                )
            )
    return cases


def _check_cases(
    cases: Sequence[GoldenCase],
    component_of: Mapping[str, int],
    errors: list[str],
) -> None:
    seen_ids: set[str] = set()
    for case in cases:
        if case.case_id in seen_ids:
            errors.append(f"duplicate case_id: {case.case_id}")
        seen_ids.add(case.case_id)
        if not case.query.strip():
            errors.append(f"case {case.case_id}: empty query")
        if case.expected_empty:
            if case.relevant_canonicals:
                errors.append(f"case {case.case_id}: expected empty but has relevant labels")
        elif not case.relevant_canonicals:
            errors.append(f"case {case.case_id}: positive case has no relevant labels")

    canonical_splits: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        for canonical in case.relevant_canonicals:
            canonical_splits[canonical].add(case.split)
    leaked = sorted(canonical for canonical, splits in canonical_splits.items() if len(splits) > 1)
    if leaked:
        errors.append(f"canonical leakage across splits: {', '.join(leaked[:5])}")

    bucket_splits: dict[str, set[str]] = defaultdict(set)
    bucket_components: dict[str, set[int]] = defaultdict(set)
    for case in cases:
        bucket_splits[case.bucket].add(case.split)
        bucket_components[case.bucket].add(component_of[case.seed_id])
    for bucket, roots in bucket_components.items():
        if len(roots) >= 2 and bucket_splits[bucket] != {"dev", "test"}:
            errors.append(
                f"bucket {bucket!r} spans {len(roots)} independent components but "
                f"only split(s) {sorted(bucket_splits[bucket])}"
            )


def _manifest(
    version: str,
    catalog_sha256: str,
    seeds: Sequence[EvalSeed],
    cases: Sequence[GoldenCase],
    quotas: Mapping[str, int],
) -> GoldenManifest:
    bucket_counts = Counter(case.bucket for case in cases)
    bucket_split_counts: dict[str, dict[str, int]] = {}
    for bucket in bucket_counts:
        bucket_split_counts[bucket] = dict(
            Counter(case.split for case in cases if case.bucket == bucket)
        )
    return GoldenManifest(
        version=version,
        generator_version=GENERATOR_VERSION,
        catalog_sha256=catalog_sha256,
        cases_sha256="",
        seed_count=len(seeds),
        case_count=len(cases),
        split_counts=dict(Counter(case.split for case in cases)),
        bucket_counts=dict(bucket_counts),
        bucket_split_counts=bucket_split_counts,
        config={
            "split_target": SPLIT_TARGET,
            "k": K,
            "limit": LIMIT,
            "bucket_quotas": dict(quotas),
        },
    )


def _cases_sha256(cases: Sequence[GoldenCase]) -> str:
    payload = [case.model_dump(mode="json") for case in cases]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

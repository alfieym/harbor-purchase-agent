"""S12 metric definitions.

Pure functions only: no search client, no catalog, no clock. Each docstring
states the exact contract the tests pin, because a metric that drifts by a
rounding rule or a duplicate would silently change a release gate.

Ranking metrics work on **canonical product ids**. The catalog groups up to 27
platform/currency variants under one canonical; the retriever already dedupes
them, and relevance is defined per canonical, so both sides speak the same
unit.
"""

import math
from collections.abc import Iterable, Sequence

__all__ = [
    "macro_mean",
    "ndcg_at_k",
    "p95_nearest_rank",
    "precision_at_k",
    "recall_at_k",
]


def ndcg_at_k(relevant: set[str], ranked: Sequence[str], k: int = 5) -> float:
    """Binary-relevance NDCG over the first ``k`` displayed canonicals.

    - A relevant canonical earns gain 1 the first time it appears.
    - A repeated canonical earns 0 but **still consumes its slot** — no
      shifting later items up.
    - The prefix is ``ranked[:k]``; items beyond ``k`` are not backfilled.
    - IDCG sums the first ``min(k, len(relevant))`` ideal positions.
    - No relevant canonicals → 0.0 (the caller should not average this case
      into a positive-case relevance score; it is a label defect).
    """
    prefix = list(ranked[:k])
    if not relevant or not prefix:
        return 0.0

    seen: set[str] = set()
    dcg = 0.0
    for position, canonical in enumerate(prefix, start=1):
        if canonical in relevant and canonical not in seen:
            dcg += 1.0 / _log2(position + 1)
        seen.add(canonical)

    ideal_positions = min(k, len(relevant))
    idcg = sum(1.0 / _log2(position + 1) for position in range(1, ideal_positions + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def recall_at_k(relevant: set[str], ranked: Sequence[str], k: int = 5) -> float:
    """Unique relevant canonicals in ``ranked[:k]`` / all relevant canonicals.

    More than ``k`` relevant labels means 1.0 is unreachable at ``k``; that is
    the honest reading, not a defect. No relevant labels → 0.0.
    """
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def precision_at_k(relevant: set[str], ranked: Sequence[str], k: int = 5) -> float:
    """Unique relevant canonicals / **actually displayed** entries in ``ranked[:k]``.

    Duplicates occupy a denominator slot and add no numerator credit. A
    positive case that returns nothing scores 0.0 rather than being skipped.
    """
    prefix = list(ranked[:k])
    if not prefix or not relevant:
        return 0.0
    return len(set(prefix) & relevant) / len(prefix)


def p95_nearest_rank(values: Sequence[float]) -> float | None:
    """95th percentile by the nearest-rank method (ceil(0.95·N)-th value).

    Nearest rank is deterministic and needs no interpolation, which is what a
    release gate wants. No samples → ``None``, never 0: "no data" and "0 ms"
    are different facts.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * 95 // 100))  # ceil(0.95 * n) in integers
    return ordered[rank - 1]


def macro_mean(values: Iterable[float | None]) -> float | None:
    """Unweighted mean over the non-``None`` values; ``None`` when none remain.

    ``None`` is used for "not applicable" (an empty-case metric on a
    non-empty case), so it must not be folded in as zero.
    """
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _log2(value: int) -> float:
    return math.log2(value)

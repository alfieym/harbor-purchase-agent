"""S12 metric definitions, pinned by counter-examples.

Every test here names the production change that would make it fail. The
awkward cases — duplicates, >5 labels, no results — are the point: those are
where a metric quietly redefines itself to look better.
"""

import math

import pytest

from harbor.eval.metrics import (
    macro_mean,
    ndcg_at_k,
    p95_nearest_rank,
    precision_at_k,
    recall_at_k,
)


def test_ndcg_perfect_ranking_is_one():
    assert ndcg_at_k({"a", "b"}, ["a", "b"]) == pytest.approx(1.0)


def test_ndcg_penalizes_a_relevant_item_at_rank_two():
    # DCG = 1/log2(3); IDCG = 1 for one relevant canonical.
    expected = 1.0 / math.log2(3)
    assert ndcg_at_k({"a"}, ["x", "a"]) == pytest.approx(expected)


def test_ndcg_duplicate_canonical_gains_nothing_but_consumes_a_slot():
    # ranks: a(gain 1), a(gain 0, still rank 2), b(gain 1 at rank 3)
    dcg = 1.0 + 0.0 + 1.0 / math.log2(4)
    idcg = 1.0 + 1.0 / math.log2(3)
    assert ndcg_at_k({"a", "b"}, ["a", "a", "b"]) == pytest.approx(dcg / idcg)


def test_ndcg_truncates_to_five_without_backfilling():
    # Both relevant items sit at ranks 6 and 7; @5 sees nothing.
    assert ndcg_at_k({"a", "b"}, ["x", "y", "z", "u", "v", "a", "b"]) == 0.0


def test_ndcg_idcg_uses_five_even_when_more_items_are_relevant():
    # Seven relevant canonicals, but only five slots: a perfect prefix is 1.0.
    assert ndcg_at_k(set("abcdefg"), list("abcde")) == pytest.approx(1.0)


def test_ndcg_is_zero_when_the_case_has_no_relevant_canonical():
    assert ndcg_at_k(set(), ["a", "b"]) == 0.0


def test_recall_counts_unique_relevant_items_in_the_first_five():
    # b appears twice but counts once.
    assert recall_at_k({"a", "b", "c"}, ["a", "x", "b", "b"]) == pytest.approx(2 / 3)


def test_recall_over_five_labels_cannot_reach_one_at_five():
    relevant = set("abcdefg")
    assert recall_at_k(relevant, list("abcde")) == pytest.approx(5 / 7)


def test_recall_is_zero_when_the_case_has_no_relevant_canonical():
    assert recall_at_k(set(), ["a"]) == 0.0


def test_precision_counts_duplicates_in_the_denominator_without_credit():
    # 2 unique relevant out of 3 displayed, even though b is relevant.
    assert precision_at_k({"a", "b"}, ["a", "a", "b"]) == pytest.approx(2 / 3)


def test_precision_uses_actual_displayed_count_when_fewer_than_five():
    assert precision_at_k({"a"}, ["a", "x"]) == pytest.approx(1 / 2)


def test_precision_of_zero_results_for_a_positive_case_is_zero():
    assert precision_at_k({"a"}, []) == 0.0


def test_p95_nearest_rank_uses_the_ceil_rank():
    values = [float(i) for i in range(1, 21)]
    # 0.95 * 20 = 19 exactly → the 19th value.
    assert p95_nearest_rank(values) == pytest.approx(19.0)
    assert p95_nearest_rank([10.0, 20.0]) == pytest.approx(20.0)


def test_p95_of_a_single_sample_is_that_sample():
    assert p95_nearest_rank([42.0]) == pytest.approx(42.0)


def test_p95_of_no_samples_is_none():
    assert p95_nearest_rank([]) is None


def test_macro_mean_skips_none_and_is_none_when_empty():
    assert macro_mean([1.0, None, 3.0]) == pytest.approx(2.0)
    assert macro_mean([None]) is None
    assert macro_mean([]) is None

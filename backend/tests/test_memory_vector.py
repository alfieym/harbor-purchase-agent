"""S8: vector encoding helpers for the SQLite long-term memory."""

import pytest

from harbor.features.memory.vector import cosine, pack, unpack


def test_pack_round_trips_float32() -> None:
    values = [0.5, -1.25, 3.0]
    assert unpack(pack(values)) == [0.5, -1.25, 3.0]


def test_pack_uses_four_bytes_per_value() -> None:
    assert len(pack([0.0] * 7)) == 28


def test_pack_of_an_empty_vector_round_trips() -> None:
    assert unpack(pack([])) == []


def test_cosine_of_identical_vectors_is_one() -> None:
    assert cosine([1.0, 2.0], [1.0, 2.0]) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_of_opposite_vectors_is_negative_one() -> None:
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_of_mismatched_lengths_is_negative_one() -> None:
    assert cosine([1.0, 2.0], [1.0]) == -1.0


def test_cosine_of_zero_vector_is_zero() -> None:
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine([1.0, 0.0], [0.0, 0.0]) == 0.0

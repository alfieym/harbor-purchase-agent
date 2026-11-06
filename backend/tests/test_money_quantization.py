"""S10: currency-aware quantization for order pricing."""

from decimal import Decimal

from harbor.features.catalog.models.money import minor_unit_exponent, quantize_major


def test_jpy_quantizes_to_whole_yen() -> None:
    assert minor_unit_exponent("JPY") == 0
    assert quantize_major(Decimal("1234.6"), "JPY") == Decimal("1235")


def test_two_decimal_currencies_round_half_up() -> None:
    assert quantize_major(Decimal("1.005"), "CNY") == Decimal("1.01")
    assert quantize_major(Decimal("89.004"), "USD") == Decimal("89.00")


def test_unknown_currency_defaults_to_two_decimals() -> None:
    assert minor_unit_exponent("XYZ") == 2

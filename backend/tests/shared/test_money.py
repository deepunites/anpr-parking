"""I-13: деньги — целые минорные единицы; property-based на ассоциативность и отсутствие потери копеек."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from parking.shared.money import Currency, Money, MoneyError, Rounding

amounts = st.integers(min_value=-(10**15), max_value=10**15).map(Money)


@given(amounts, amounts, amounts)
def test_addition_is_associative_and_commutative(a: Money, b: Money, c: Money) -> None:
    assert (a + b) + c == a + (b + c)
    assert a + b == b + a


@given(st.integers(min_value=0, max_value=10**12), st.integers(min_value=1, max_value=97))
def test_split_never_loses_or_creates_minor_units(minor: int, parts: int) -> None:
    pieces = Money(minor).split(parts)
    assert len(pieces) == parts
    assert sum(p.minor for p in pieces) == minor
    assert max(p.minor for p in pieces) - min(p.minor for p in pieces) <= 1


@given(
    st.integers(min_value=0, max_value=10**12), st.integers(min_value=0, max_value=100), st.sampled_from(list(Rounding))
)
def test_discount_and_net_always_sum_to_total(minor: int, percent: int, rounding: Rounding) -> None:
    total = Money(minor)
    discount = total.percent(percent, rounding)
    net = total - discount
    assert discount + net == total
    assert Money.zero() <= discount <= total


def test_thirty_three_percent_of_1000_tiyin() -> None:
    total = Money(1000)
    for rounding in Rounding:
        assert total.percent(33, rounding) == Money(330)
        assert total - total.percent(33, rounding) == Money(670)
    assert Money(1001).percent(33, Rounding.DOWN) == Money(330)
    assert Money(1001).percent(33, Rounding.UP) == Money(331)
    assert Money(1001).percent(33, Rounding.HALF_UP) == Money(330)


def test_rounding_modes_on_half() -> None:
    half = Fraction(1, 2)
    assert Money(1).multiply(half, Rounding.UP) == Money(1)
    assert Money(1).multiply(half, Rounding.DOWN) == Money(0)
    assert Money(1).multiply(half, Rounding.HALF_UP) == Money(1)
    assert Money(1).multiply(half, Rounding.HALF_EVEN) == Money(0)
    assert Money(3).multiply(half, Rounding.HALF_EVEN) == Money(2)
    assert Money(-1).multiply(half, Rounding.UP) == Money(0)
    assert Money(-1).multiply(half, Rounding.DOWN) == Money(-1)


def test_decimal_factor_is_exact() -> None:
    assert Money(1000).multiply(Decimal("1.5"), Rounding.HALF_UP) == Money(1500)
    assert Money(1000).multiply(Decimal("0.333"), Rounding.HALF_UP) == Money(333)


def test_float_and_int_conversions_are_forbidden() -> None:
    m = Money(1500000)
    with pytest.raises(TypeError):
        float(m)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        int(m)  # type: ignore[call-overload]
    with pytest.raises(MoneyError):
        m.multiply(0.5, Rounding.UP)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        _ = m * 0.5  # type: ignore[operator]
    with pytest.raises(MoneyError):
        Money(1.0)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        Money(True)


def test_currency_mismatch_and_bad_operands() -> None:
    with pytest.raises(MoneyError):
        _ = Money(1) + 1  # type: ignore[operator]
    assert Money(5) * 3 == Money(15)
    assert 3 * Money(5) == Money(15)


def test_formatting_for_ui() -> None:
    assert Money(1500000).formatted() == "15 000,00 сум"
    assert Money(1500000).formatted("uz") == "15 000,00 so‘m"
    assert Money(5).formatted() == "0,05 сум"
    assert Money(-123456789).formatted() == "-1 234 567,89 сум"
    assert Money(1500000).to_dict() == {"minor": 1500000, "currency": "UZS", "formatted": "15 000,00 сум"}
    assert Money(0, Currency.UZS).is_zero

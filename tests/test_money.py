"""Money primitives. The point of these tests is that a float cannot get in."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmx.core.money import (
    MoneyTypeError,
    Probability,
    Usd,
    cents_to_probability,
    notional,
    probability_to_cents,
)


class TestFloatsAreRejected:
    """§3: 'A test must fail if a float reaches an order.' These are those tests."""

    @pytest.mark.parametrize("value", [0.1, 1.0, -3.5, 1e-9])
    def test_usd_rejects_float(self, value: float) -> None:
        with pytest.raises(MoneyTypeError, match="float is not permitted"):
            Usd(value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [0.5, 0.0, 1.0])
    def test_probability_rejects_float(self, value: float) -> None:
        with pytest.raises(MoneyTypeError, match="float is not permitted"):
            Probability(value)  # type: ignore[arg-type]

    def test_usd_multiplication_rejects_float(self) -> None:
        with pytest.raises(MoneyTypeError):
            Usd("10") * 1.5  # type: ignore[operator]

    def test_bool_is_not_a_number(self) -> None:
        # bool subclasses int; True must not silently become $1.
        with pytest.raises(MoneyTypeError, match="bool"):
            Usd(True)  # type: ignore[arg-type]

    def test_notional_rejects_float_quantity(self) -> None:
        with pytest.raises(MoneyTypeError):
            notional(Probability("0.5"), 10.0)  # type: ignore[arg-type]

    def test_nan_and_infinity_rejected(self) -> None:
        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(MoneyTypeError, match="finite"):
                Usd(bad)


class TestUsdArithmetic:
    def test_addition_is_exact(self) -> None:
        # The canonical float failure: 0.1 + 0.2 != 0.3.
        assert Usd("0.1") + Usd("0.2") == Usd("0.3")

    def test_cannot_add_foreign_types(self) -> None:
        with pytest.raises(MoneyTypeError):
            Usd("1") + 1  # type: ignore[operator]
        with pytest.raises(MoneyTypeError):
            Usd("1") + Probability("0.5")  # type: ignore[operator]

    def test_cannot_compare_to_foreign_types(self) -> None:
        with pytest.raises(MoneyTypeError):
            _ = Usd("1") < 2  # type: ignore[operator]

    def test_ratio_is_dimensionless(self) -> None:
        assert Usd("50").ratio_to(Usd("200")) == Decimal("0.25")

    def test_ratio_by_zero_raises(self) -> None:
        with pytest.raises(ZeroDivisionError):
            Usd("1").ratio_to(Usd.zero())

    def test_division_by_zero_raises(self) -> None:
        with pytest.raises(ZeroDivisionError):
            Usd("1") / 0

    def test_fees_round_up_to_the_cent_against_us(self) -> None:
        # $0.0175/contract * 1 contract is a fraction of a cent; the venue takes a
        # whole cent. Rounding to nearest would understate every small-order fee.
        assert Usd("0.0175").to_cents_ceil() == 2
        assert Usd("0.0175").to_cents_floor() == 1

    def test_immutable(self) -> None:
        amount = Usd("5")
        with pytest.raises(AttributeError):
            amount._v = Decimal("1000")  # type: ignore[misc]

    def test_str_is_human_readable_but_repr_is_exact(self) -> None:
        assert str(Usd("1234.5")) == "$1234.50"
        assert "1234.5" in repr(Usd("1234.5"))


class TestProbability:
    @pytest.mark.parametrize("value", ["-0.01", "1.01", "2", "-1"])
    def test_out_of_range_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            Probability(value)

    def test_complement(self) -> None:
        assert Probability("0.37").complement() == Probability("0.63")

    def test_edge_is_signed(self) -> None:
        assert Probability("0.55").edge_over(Probability("0.40")) == Decimal("0.15")
        assert Probability("0.40").edge_over(Probability("0.55")) == Decimal("-0.15")

    def test_cannot_compare_to_usd(self) -> None:
        with pytest.raises(MoneyTypeError):
            _ = Probability("0.5") < Usd("0.5")  # type: ignore[operator]


class TestVenueBoundaryConversion:
    def test_kalshi_cents_round_trip(self) -> None:
        for cents in range(0, 101):
            assert probability_to_cents(cents_to_probability(cents)) == cents

    def test_off_tick_price_is_an_error_not_a_rounding(self) -> None:
        # Rounding here would place an order at a price the strategy never authorized.
        with pytest.raises(ValueError, match="1-cent tick"):
            probability_to_cents(Probability("0.405"))

    @pytest.mark.parametrize("cents", [-1, 101, 1000])
    def test_out_of_range_cents_rejected(self, cents: int) -> None:
        with pytest.raises(ValueError):
            cents_to_probability(cents)


class TestNotional:
    def test_cost_equals_max_loss(self) -> None:
        # The identity every exposure limit depends on.
        assert notional(Probability("0.40"), 100) == Usd("40")

    def test_zero_quantity_is_free(self) -> None:
        assert notional(Probability("0.40"), 0) == Usd.zero()

    def test_negative_quantity_rejected(self) -> None:
        with pytest.raises(ValueError):
            notional(Probability("0.40"), -1)

    @given(
        cents=st.integers(min_value=1, max_value=99),
        quantity=st.integers(min_value=0, max_value=100_000),
    )
    def test_notional_never_exceeds_quantity_dollars(self, cents: int, quantity: int) -> None:
        # A contract can never cost more than $1, so notional <= quantity dollars.
        assert notional(cents_to_probability(cents), quantity) <= Usd(quantity)

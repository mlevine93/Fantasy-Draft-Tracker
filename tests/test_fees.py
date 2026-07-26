"""Fee model.

The fee schedules could not be verified from primary documentation (docs/api-notes.md
§0), so these tests are about the *defenses* around that, not about matching a published
number: the model must round against us, it must never under-estimate, and a venue that
charges more than we modeled must halt rather than warn.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pmx.core.models import Venue
from pmx.core.money import Probability, Usd
from pmx.venues.fees import FeeDivergence, FeeModel, assert_fee_model_matches
from tests.conftest import make_fee_model_config

prices = st.integers(min_value=1, max_value=99).map(lambda c: Probability(Decimal(c) / 100))


@pytest.fixture
def model() -> FeeModel:
    return FeeModel(Venue.KALSHI, make_fee_model_config())


class TestShape:
    def test_fee_peaks_at_the_coin_flip(self, model: FeeModel) -> None:
        at_half = model.estimate(Probability("0.50"), 1000, is_maker=False)
        at_tail = model.estimate(Probability("0.05"), 1000, is_maker=False)
        assert at_half > at_tail

    def test_maker_is_cheaper_than_taker(self, model: FeeModel) -> None:
        price = Probability("0.50")
        assert model.estimate(price, 1000, is_maker=True) < model.estimate(
            price, 1000, is_maker=False
        )

    def test_per_contract_cap_binds(self) -> None:
        uncapped = make_fee_model_config().model_copy(
            update={"taker_coefficient": Decimal("10"), "per_contract_cap": Usd("0.01")}
        )
        model = FeeModel(Venue.KALSHI, uncapped)
        # 10 * 0.5 * 0.5 = $2.50/contract before the cap; $0.01 after.
        assert model.estimate(Probability("0.50"), 100, is_maker=False) == Usd("1.00")

    def test_rounds_up_to_the_cent(self, model: FeeModel) -> None:
        """0.07 * 0.5 * 0.5 = $0.0175 for one contract. The venue takes two cents."""
        assert model.estimate(Probability("0.50"), 1, is_maker=False) == Usd("0.02")

    def test_zero_quantity_is_an_error_not_a_free_trade(self, model: FeeModel) -> None:
        with pytest.raises(ValueError):
            model.estimate(Probability("0.50"), 0, is_maker=False)

    def test_round_trip_charges_both_legs(self, model: FeeModel) -> None:
        one_leg = model.estimate(Probability("0.50"), 100, is_maker=False)
        both = model.round_trip_estimate(
            Probability("0.50"), Probability("0.50"), 100, is_maker=False
        )
        assert both == one_leg + one_leg


class TestSafetyMultiplier:
    def test_multiplier_scales_the_estimate_upward(self) -> None:
        base = FeeModel(Venue.KALSHI, make_fee_model_config())
        padded = FeeModel(
            Venue.KALSHI,
            make_fee_model_config().model_copy(update={"safety_multiplier": Decimal("2")}),
        )
        price = Probability("0.40")
        assert padded.estimate(price, 100, is_maker=False) == base.estimate(
            price, 100, is_maker=False
        ) * Decimal(2)

    @settings(max_examples=200, deadline=None)
    @given(price=prices, quantity=st.integers(min_value=1, max_value=100_000))
    def test_estimate_is_never_below_the_unpadded_formula(self, price, quantity) -> None:
        """The model may only ever err expensive. Erring cheap is what invents edge."""
        config = make_fee_model_config().model_copy(
            update={"safety_multiplier": Decimal("1.5")}
        )
        model = FeeModel(Venue.KALSHI, config)
        raw = config.taker_coefficient * price.value * (Decimal(1) - price.value) * quantity
        assert model.estimate(price, quantity, is_maker=False).amount >= min(
            raw, config.per_contract_cap.amount * quantity
        )


class TestDivergenceHalt:
    def test_charging_more_than_modeled_raises(self) -> None:
        with pytest.raises(FeeDivergence, match="dangerous direction"):
            assert_fee_model_matches(Usd("1.00"), Usd("1.50"), tolerance=Usd("0.01"))

    def test_charging_less_than_modeled_is_fine(self) -> None:
        """Asymmetric on purpose: conservative is the intended bias, not an error."""
        assert_fee_model_matches(Usd("1.00"), Usd("0.50"), tolerance=Usd("0.01"))

    def test_within_tolerance_is_fine(self) -> None:
        assert_fee_model_matches(Usd("1.00"), Usd("1.005"), tolerance=Usd("0.01"))

    def test_one_cent_over_tolerance_still_halts(self) -> None:
        with pytest.raises(FeeDivergence):
            assert_fee_model_matches(Usd("1.00"), Usd("1.02"), tolerance=Usd("0.01"))

"""Boundary bugs: identifier confusion, timezones, and clock skew.

Named in §9 as explicit test requirements because each one produces a well-formed request
against the wrong thing, which is the class of bug that costs money without erroring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from pmx.core.clock import (
    MAX_TOLERATED_SKEW,
    Clock,
    StaleTimestampError,
    require_fresh,
    utc_now,
)
from pmx.core.ids import ConditionId, IdFormatError, TokenId, condition_id, token_id
from pmx.core.models import Market, OutcomeRef, Quote, QuoteSource, Side, Venue
from pmx.core.money import Probability, Usd
from tests.conftest import NOW

CONDITION = "0x" + "a" * 64
TOKEN_DECIMAL = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


def _signal_like(signal, **changes):
    """Rebuild a Signal through its constructor.

    `model_copy(update=...)` does NOT re-run model validators, so it can produce an
    object that violates its own invariants. That is fine for building test fixtures,
    but useless for testing the invariants themselves — and it is why production code
    is forbidden from using it (see tests/test_single_order_path.py).
    """
    return type(signal)(**{**signal.model_dump(), **changes})


def _quote_like(quote, **changes):
    return type(quote)(**{**quote.model_dump(), **changes})


class TestConditionIdVersusTokenId:
    """The #1 Polymarket integration bug (docs/api-notes.md §2.4)."""

    def test_condition_id_parses(self) -> None:
        assert condition_id(CONDITION) == ConditionId(CONDITION)

    def test_token_id_accepts_decimal_uint256(self) -> None:
        assert token_id(TOKEN_DECIMAL) == TokenId(TOKEN_DECIMAL)

    def test_a_condition_id_is_not_a_token_id_at_the_type_level(self) -> None:
        """Runtime cannot distinguish two NewTypes over str — `mypy --strict` can, and
        does, at every call site. This test documents the runtime half: the parsers
        have different shapes, so a decimal token ID cannot pass as a condition ID."""
        with pytest.raises(IdFormatError, match="must start with 0x"):
            condition_id(TOKEN_DECIMAL)

    def test_truncated_hex_rejected(self) -> None:
        with pytest.raises(IdFormatError, match="chars"):
            condition_id("0xdeadbeef")

    def test_non_hex_rejected(self) -> None:
        with pytest.raises(IdFormatError, match="valid hex"):
            condition_id("0x" + "z" * 64)

    def test_empty_token_rejected(self) -> None:
        with pytest.raises(IdFormatError, match="empty"):
            token_id("")

    def test_arbitrary_string_is_not_a_token_id(self) -> None:
        with pytest.raises(IdFormatError):
            token_id("will-trump-win")


class TestTimezones:
    def test_naive_close_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            Market(
                venue=Venue.KALSHI,
                market_key="M",
                event_key="E",
                question="Q",
                close_time=datetime(2026, 9, 1, 12, 0),
                resolution_source="src",
            )

    def test_naive_quote_timestamp_is_rejected(self) -> None:
        outcome = OutcomeRef(venue=Venue.KALSHI, market_key="M", outcome_key="O", event_key="E")
        with pytest.raises(ValueError, match="timezone-aware"):
            Quote(
                outcome=outcome,
                bid=Probability("0.4"),
                ask=Probability("0.5"),
                observed_at=datetime(2026, 9, 1, 12, 0),
                source=QuoteSource.BOOK,
            )

    def test_non_utc_timezone_is_accepted_and_compared_correctly(self) -> None:
        """A market closing at 20:00 UTC-4 has not closed at 23:00 UTC. Comparing
        aware datetimes handles this; the danger is only ever naive ones."""
        eastern = timezone(timedelta(hours=-4))
        close = datetime(2026, 9, 1, 20, 0, tzinfo=eastern)
        assert close > datetime(2026, 9, 1, 23, 0, tzinfo=UTC)

    def test_utc_now_is_always_aware(self) -> None:
        assert utc_now().tzinfo is not None


class TestClockSkew:
    def test_no_observation_means_no_correction(self) -> None:
        clock = Clock()
        assert clock.skew_for("kalshi") == timedelta(0)

    def test_small_skew_is_recorded_and_applied(self) -> None:
        clock = Clock()
        clock.observe_venue_time("kalshi", utc_now() + timedelta(seconds=2))
        assert clock.skew_for("kalshi") > timedelta(seconds=1)
        assert clock.now_for("kalshi") > utc_now()

    def test_large_skew_raises_rather_than_being_papered_over(self) -> None:
        clock = Clock()
        with pytest.raises(StaleTimestampError, match="skew"):
            clock.observe_venue_time("kalshi", utc_now() + MAX_TOLERATED_SKEW * 3)

    def test_naive_venue_time_rejected(self) -> None:
        clock = Clock()
        with pytest.raises(ValueError, match="timezone-aware"):
            clock.observe_venue_time("kalshi", datetime(2026, 7, 26, 12, 0))

    def test_kalshi_signs_in_milliseconds_and_polymarket_in_seconds(self) -> None:
        """The asymmetry from docs/api-notes.md §3. Getting it backwards is a 401 that
        looks exactly like a credential problem."""
        clock = Clock()
        millis = clock.epoch_millis_for("kalshi")
        seconds = clock.epoch_seconds_for("polymarket")
        assert millis // 1000 == pytest.approx(seconds, abs=1)
        assert millis > seconds * 100


class TestFreshness:
    def test_fresh_timestamp_passes(self) -> None:
        require_fresh(utc_now(), max_age=timedelta(seconds=10), what="quote")

    def test_old_timestamp_raises(self) -> None:
        with pytest.raises(StaleTimestampError, match="old"):
            require_fresh(
                utc_now() - timedelta(seconds=60), max_age=timedelta(seconds=10), what="quote"
            )

    def test_future_timestamp_is_an_error_not_maximum_freshness(self) -> None:
        """Otherwise a wrong clock defeats every staleness check in the system."""
        with pytest.raises(StaleTimestampError, match="future"):
            require_fresh(
                utc_now() + timedelta(hours=1), max_age=timedelta(seconds=10), what="quote"
            )

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            require_fresh(datetime(2026, 7, 26, 12, 0), max_age=timedelta(seconds=10), what="q")


class TestModelInvariants:
    def test_crossed_book_is_rejected(self) -> None:
        outcome = OutcomeRef(venue=Venue.KALSHI, market_key="M", outcome_key="O", event_key="E")
        with pytest.raises(ValueError, match="crossed book"):
            Quote(
                outcome=outcome,
                bid=Probability("0.60"),
                ask=Probability("0.40"),
                observed_at=NOW,
                source=QuoteSource.BOOK,
            )

    def test_signal_quote_must_describe_the_signal_outcome(self, signal, outcome) -> None:
        other = OutcomeRef(
            venue=Venue.KALSHI, market_key="OTHER", outcome_key="OTHER:YES", event_key="OTHER"
        )
        with pytest.raises(ValueError, match="does not match"):
            _signal_like(signal, quote=_quote_like(signal.quote, outcome=other))

    def test_buy_limit_above_thesis_is_rejected_as_a_strategy_bug(self, signal) -> None:
        with pytest.raises(ValueError, match="negative edge"):
            _signal_like(signal, limit_price=Probability("0.90"))

    def test_sell_limit_below_thesis_is_rejected(self, signal) -> None:
        with pytest.raises(ValueError, match="negative edge"):
            _signal_like(
                signal,
                side=Side.SELL,
                thesis_price=Probability("0.80"),
                limit_price=Probability("0.40"),
            )

    def test_domain_objects_are_frozen(self, signal) -> None:
        with pytest.raises(ValueError, match="frozen|immutable"):
            signal.max_quantity = 999

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            OutcomeRef(
                venue=Venue.KALSHI,
                market_key="M",
                outcome_key="O",
                event_key="E",
                typo_field="x",
            )

    def test_unknown_volume_is_representable(self) -> None:
        """None must be distinguishable from zero volume: one is ignorance, the other
        is a fact, and the risk engine treats both as illiquid for that reason."""
        market = Market(
            venue=Venue.KALSHI,
            market_key="M",
            event_key="E",
            question="Q",
            close_time=NOW,
            resolution_source="s",
        )
        assert market.volume_24h is None
        assert Market(
            venue=Venue.KALSHI,
            market_key="M",
            event_key="E",
            question="Q",
            close_time=NOW,
            resolution_source="s",
            volume_24h=Usd.zero(),
        ).volume_24h == Usd.zero()

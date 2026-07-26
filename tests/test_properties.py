"""Property-based tests (§9).

The claim being tested is the one that matters: **no sequence of valid inputs produces
an order that exceeds a limit.** Example-based tests prove the limits fire on the cases I
thought of. These try to prove there are no others.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pmx.core.models import (
    DepthLevel,
    Market,
    OutcomeRef,
    PromotionState,
    Quote,
    QuoteSource,
    Side,
    Signal,
    Venue,
)
from pmx.core.money import Probability, Usd, notional
from pmx.risk.engine import DecisionOutcome, RiskEngine
from pmx.risk.limits import Limit
from pmx.risk.sizing import kelly_fraction_for, size_position
from pmx.risk.state import PortfolioSnapshot
from pmx.venues.fees import FeeModel
from tests.conftest import NOW, make_fee_model_config

SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

prices = st.integers(min_value=1, max_value=99).map(lambda c: Probability(Decimal(c) / 100))
usd_amounts = st.integers(min_value=0, max_value=10_000_000).map(lambda c: Usd(Decimal(c) / 100))


def build_case(
    *,
    thesis: Probability,
    limit_price: Probability,
    side: Side,
    quantity: int,
    depth: int,
    equity: Usd,
    cash: Usd,
    deployed_total: Usd,
    deployed_outcome: Usd,
    deployed_event: Usd,
    deployed_venue: Usd,
    day_new_capital: Usd,
) -> tuple[Signal, Market, PortfolioSnapshot]:
    outcome = OutcomeRef(
        venue=Venue.KALSHI, market_key="M", outcome_key="M:YES", event_key="E"
    )
    book_level = DepthLevel(price=limit_price, quantity=depth)
    quote = Quote(
        outcome=outcome,
        bid=limit_price,
        ask=limit_price,
        bid_depth=(book_level,),
        ask_depth=(book_level,),
        observed_at=NOW,
        source=QuoteSource.BOOK,
    )
    signal = Signal(
        signal_id="p-1",
        strategy="manual",
        outcome=outcome,
        side=side,
        thesis_price=thesis,
        limit_price=limit_price,
        max_quantity=quantity,
        rationale="property test",
        created_at=NOW,
        quote=quote,
    )
    market = Market(
        venue=Venue.KALSHI,
        market_key="M",
        event_key="E",
        question="Q",
        close_time=NOW + timedelta(days=10),
        resolution_source="src",
        correlation_tags=("tag",),
        volume_24h=Usd("1000000"),
    )
    snapshot = PortfolioSnapshot(
        equity=equity,
        cash=cash,
        peak_equity=equity,
        deployed_total=deployed_total,
        deployed_by_venue={Venue.KALSHI: deployed_venue},
        deployed_by_event={"E": deployed_event},
        deployed_by_correlation_tag={"tag": deployed_event},
        deployed_by_outcome={"M:YES": deployed_outcome},
        day_new_capital=day_new_capital,
        last_reconciled_at=NOW - timedelta(seconds=1),
        reconciliation_ok=True,
        strategy_states={"manual": PromotionState.LIVE},
    )
    return signal, market, snapshot


class TestNoOrderEverExceedsALimit:
    @SETTINGS
    @given(
        thesis=prices,
        limit_price=prices,
        quantity=st.integers(min_value=1, max_value=1_000_000),
        depth=st.integers(min_value=1, max_value=1_000_000),
        equity=usd_amounts,
        cash=usd_amounts,
        deployed_total=usd_amounts,
        deployed_outcome=usd_amounts,
        deployed_event=usd_amounts,
        deployed_venue=usd_amounts,
        day_new_capital=usd_amounts,
        side=st.sampled_from([Side.BUY, Side.SELL]),
    )
    def test_approved_orders_respect_every_capital_limit(
        self,
        config,
        thesis,
        limit_price,
        quantity,
        depth,
        equity,
        cash,
        deployed_total,
        deployed_outcome,
        deployed_event,
        deployed_venue,
        day_new_capital,
        side,
    ) -> None:
        # The Signal model itself forbids a limit worse than the thesis, so skip those:
        # they are rejected before the engine sees them.
        if side is Side.BUY:
            assume(limit_price <= thesis)
        else:
            assume(limit_price >= thesis)

        engine = RiskEngine(
            config,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        signal, market, snapshot = build_case(
            thesis=thesis,
            limit_price=limit_price,
            side=side,
            quantity=quantity,
            depth=depth,
            equity=equity,
            cash=cash,
            deployed_total=deployed_total,
            deployed_outcome=deployed_outcome,
            deployed_event=deployed_event,
            deployed_venue=deployed_venue,
            day_new_capital=day_new_capital,
        )

        decision = engine.evaluate(signal, market, snapshot, now=NOW)
        if decision.outcome is DecisionOutcome.REJECTED:
            return

        order = decision.order
        assert order is not None
        cost = notional(order.limit_price, order.quantity)

        # Every limit, re-checked independently of the engine's own arithmetic.
        assert deployed_outcome + cost <= config.max_position_size
        assert deployed_total + cost <= config.max_total_deployed
        assert deployed_venue + cost <= config.max_venue_deployed[Venue.KALSHI]
        assert deployed_event + cost <= config.max_single_event_exposure
        assert deployed_event + cost <= config.max_correlated_exposure
        assert day_new_capital + cost <= config.max_daily_new_capital
        assert cash - cost >= config.min_cash_reserve
        assert order.quantity <= signal.max_quantity
        assert order.quantity <= depth
        assert equity > Usd.zero()
        assert (deployed_outcome + cost).ratio_to(equity) <= config.max_position_pct_of_bankroll

    @SETTINGS
    @given(
        thesis=prices,
        limit_price=prices,
        quantity=st.integers(min_value=1, max_value=10_000),
        depth=st.integers(min_value=1, max_value=10_000),
        equity=usd_amounts,
        side=st.sampled_from([Side.BUY, Side.SELL]),
    )
    def test_approved_orders_always_clear_the_edge_floor(
        self, config, thesis, limit_price, quantity, depth, equity, side
    ) -> None:
        if side is Side.BUY:
            assume(limit_price <= thesis)
        else:
            assume(limit_price >= thesis)

        fee_model = FeeModel(Venue.KALSHI, make_fee_model_config())
        engine = RiskEngine(config, fee_models={str(Venue.KALSHI): fee_model})
        signal, market, snapshot = build_case(
            thesis=thesis,
            limit_price=limit_price,
            side=side,
            quantity=quantity,
            depth=depth,
            equity=equity,
            cash=Usd("100000000"),
            deployed_total=Usd.zero(),
            deployed_outcome=Usd.zero(),
            deployed_event=Usd.zero(),
            deployed_venue=Usd.zero(),
            day_new_capital=Usd.zero(),
        )
        decision = engine.evaluate(signal, market, snapshot, now=NOW)
        if decision.outcome is DecisionOutcome.REJECTED:
            return

        order = decision.order
        assert order is not None
        gross = Usd(signal.raw_edge() * order.quantity)
        fee = fee_model.estimate(order.limit_price, order.quantity, is_maker=False)
        cost = notional(order.limit_price, order.quantity)
        net_bps = (gross - fee).ratio_to(cost) * Decimal(10_000)
        assert net_bps >= config.min_edge_bps_after_fees

    @SETTINGS
    @given(price=prices, side=st.sampled_from([Side.BUY, Side.SELL]))
    def test_zero_or_negative_edge_never_produces_an_order(self, config, price, side) -> None:
        """A signal with no edge must die at NO_EDGE, whatever the rest of the state is.

        A limit strictly worse than the thesis is rejected by the `Signal` model itself,
        so the case that can actually reach the engine is thesis == limit: zero edge,
        arbitrarily generous portfolio state, arbitrarily deep book.
        """
        thesis = limit_price = price
        engine = RiskEngine(
            config,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        signal, market, snapshot = build_case(
            thesis=thesis,
            limit_price=limit_price,
            side=side,
            quantity=100,
            depth=100_000,
            equity=Usd("1000000"),
            cash=Usd("1000000"),
            deployed_total=Usd.zero(),
            deployed_outcome=Usd.zero(),
            deployed_event=Usd.zero(),
            deployed_venue=Usd.zero(),
            day_new_capital=Usd.zero(),
        )
        decision = engine.evaluate(signal, market, snapshot, now=NOW)
        assert decision.outcome is DecisionOutcome.REJECTED
        assert decision.rejection is not None
        assert decision.rejection.limit is Limit.NO_EDGE


class TestKellyProperties:
    @SETTINGS
    @given(thesis=prices, price=prices)
    def test_kelly_is_never_negative_and_never_exceeds_one(self, thesis, price) -> None:
        for side in (Side.BUY, Side.SELL):
            fraction = kelly_fraction_for(thesis, price, side)
            assert 0 <= fraction <= 1

    @SETTINGS
    @given(thesis=prices, price=prices)
    def test_kelly_is_zero_exactly_when_there_is_no_edge(self, thesis, price) -> None:
        buy = kelly_fraction_for(thesis, price, Side.BUY)
        assert (buy > 0) == (thesis > price and price < Probability("1"))

    @SETTINGS
    @given(
        thesis=prices,
        price=prices,
        equity=usd_amounts,
        multiplier=st.sampled_from([Decimal("0.1"), Decimal("0.25"), Decimal("0.5"), Decimal("1")]),
        cap_pct=st.sampled_from([Decimal("0.01"), Decimal("0.05"), Decimal("0.2")]),
    )
    def test_sizing_never_exceeds_its_caps(self, thesis, price, equity, multiplier, cap_pct):
        sizing = size_position(
            thesis=thesis,
            price=price,
            side=Side.BUY,
            bankroll=equity,
            kelly_multiplier=multiplier,
            max_position_value=Usd("1000"),
            max_position_pct_of_bankroll=cap_pct,
            signal_max_quantity=10_000,
        )
        stake = notional(price, sizing.quantity)
        assert sizing.quantity <= 10_000
        assert stake <= Usd("1000")
        if equity > Usd.zero():
            assert stake.ratio_to(equity) <= cap_pct
        # Fractional Kelly can never size above full Kelly.
        assert sizing.quantity <= sizing.kelly_raw_quantity or sizing.kelly_raw_quantity == 0

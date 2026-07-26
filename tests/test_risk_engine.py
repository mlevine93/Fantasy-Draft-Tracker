"""Risk engine. Every limit gets a test that proves it rejects, by name.

These are written failure-first on purpose: the happy path is one test at the top, and
everything below it breaks exactly one thing. The happy path was never what loses money.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmx.audit.ledger import EntryKind
from pmx.core.models import (
    DepthLevel,
    PromotionState,
    Quote,
    QuoteSource,
    Side,
    Signal,
    Venue,
)
from pmx.core.money import Probability, Usd
from pmx.risk.circuit import Halt, HaltScope
from pmx.risk.engine import DecisionOutcome, RiskEngine, RiskEngineFailure
from pmx.risk.limits import Limit
from pmx.venues.fees import FeeModel
from tests.conftest import NOW, make_fee_model_config


def evaluate(engine, signal, market, snapshot, **kwargs):
    return engine.evaluate(signal, market, snapshot, now=kwargs.pop("now", NOW), **kwargs)


def assert_rejected(decision, limit: Limit) -> None:
    assert decision.outcome is DecisionOutcome.REJECTED, f"expected rejection, got {decision}"
    assert decision.rejection is not None
    assert decision.rejection.limit is limit, (
        f"expected {limit}, got {decision.rejection.one_line()}"
    )
    assert decision.order is None, "a rejected decision must never carry an order"


class TestHappyPath:
    def test_clean_signal_is_approved(self, engine, signal, market, snapshot) -> None:
        decision = evaluate(engine, signal, market, snapshot)
        assert decision.outcome is DecisionOutcome.APPROVED
        assert decision.order is not None
        assert decision.order.quantity == 100
        assert decision.order.limit_price == Probability("0.40")

    def test_approved_order_records_kelly_diagnostics(self, engine, signal, market, snapshot):
        decision = evaluate(engine, signal, market, snapshot)
        assert decision.order is not None
        # Kelly wanted far more than the signal asked for; we log both so the operator
        # can see how often the cap binds.
        assert decision.order.kelly_raw_quantity > decision.order.kelly_capped_quantity


class TestKillSwitchAndMasterSwitches:
    def test_kill_file_blocks_everything(self, config, signal, market, snapshot, tmp_path):
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        engine = RiskEngine(
            config.model_copy(update={"kill_file": str(kill)}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        assert_rejected(evaluate(engine, signal, market, snapshot), Limit.KILL_SWITCH)

    def test_kill_switch_beats_a_perfect_signal_and_an_empty_book(
        self, config, signal, market, snapshot, tmp_path
    ):
        # The kill switch must be checked before anything else can even error.
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        engine = RiskEngine(config.model_copy(update={"kill_file": str(kill)}), fee_models={})
        assert_rejected(evaluate(engine, signal, market, snapshot), Limit.KILL_SWITCH)

    def test_live_trading_disabled_by_default_config(self, config, signal, market, snapshot):
        engine = RiskEngine(
            config.model_copy(update={"live_trading_enabled": False}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        assert_rejected(evaluate(engine, signal, market, snapshot), Limit.LIVE_TRADING_DISABLED)


class TestHalts:
    def test_system_halt_blocks(self, engine, signal, market, snapshot):
        halted = snapshot.model_copy(
            update={
                "active_halts": (
                    Halt(
                        scope=HaltScope.SYSTEM,
                        limit=Limit.MAX_DRAWDOWN_HALT,
                        detail="drawdown",
                        tripped_at=NOW,
                    ),
                )
            }
        )
        assert_rejected(evaluate(engine, signal, market, halted), Limit.SYSTEM_HALTED)

    def test_strategy_halt_blocks_only_that_strategy(self, engine, signal, market, snapshot):
        halted = snapshot.model_copy(
            update={
                "active_halts": (
                    Halt(
                        scope=HaltScope.STRATEGY,
                        limit=Limit.PER_STRATEGY_LOSS_HALT,
                        detail="strategy loss",
                        tripped_at=NOW,
                        strategy="manual",
                    ),
                ),
                "strategy_states": {
                    "manual": PromotionState.LIVE,
                    "calibration": PromotionState.LIVE,
                },
            }
        )
        assert_rejected(evaluate(engine, signal, market, halted), Limit.STRATEGY_DISABLED)

        other = signal.model_copy(update={"strategy": "calibration"})
        assert evaluate(engine, other, market, halted).outcome is DecisionOutcome.APPROVED


class TestPromotionGate:
    @pytest.mark.parametrize(
        "state", [PromotionState.BACKTEST, PromotionState.PAPER, PromotionState.SHADOW]
    )
    def test_unpromoted_strategy_cannot_trade(self, engine, signal, market, snapshot, state):
        snap = snapshot.model_copy(update={"strategy_states": {"manual": state}})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.STRATEGY_NOT_LIVE)

    def test_unregistered_strategy_cannot_trade(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"strategy_states": {}})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.STRATEGY_NOT_LIVE)


class TestFeeModelGate:
    def test_unverified_fee_model_rejects_everything(self, config, signal, market, snapshot):
        engine = RiskEngine(
            config,
            fee_models={
                str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config(verified=False))
            },
        )
        assert_rejected(evaluate(engine, signal, market, snapshot), Limit.FEE_MODEL_UNVERIFIED)

    def test_missing_fee_model_rejects(self, config, signal, market, snapshot):
        engine = RiskEngine(config, fee_models={})
        assert_rejected(evaluate(engine, signal, market, snapshot), Limit.FEE_MODEL_UNVERIFIED)


class TestReconciliationGate:
    def test_diverged_state_blocks(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"reconciliation_ok": False})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.RECONCILIATION_DIVERGED)

    def test_never_reconciled_blocks(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"last_reconciled_at": None})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.RECONCILIATION_STALE)

    def test_stale_reconciliation_blocks(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(
            update={"last_reconciled_at": NOW - timedelta(seconds=3600)}
        )
        assert_rejected(evaluate(engine, signal, market, snap), Limit.RECONCILIATION_STALE)


class TestDataQuality:
    def test_indicative_price_cannot_size_a_trade(self, engine, signal, market, snapshot):
        """Gamma-style metadata prices lag the book; sizing off one is the documented bug."""
        stale_source = signal.quote.model_copy(update={"source": QuoteSource.INDICATIVE})
        sig = signal.model_copy(update={"quote": stale_source})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.QUOTE_SOURCE_NOT_BOOK)

    def test_stale_quote_blocks(self, engine, signal, market, snapshot):
        old = signal.quote.model_copy(update={"observed_at": NOW - timedelta(seconds=60)})
        sig = signal.model_copy(update={"quote": old})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.QUOTE_STALE)

    def test_empty_book_blocks(self, engine, signal, market, snapshot):
        empty = signal.quote.model_copy(update={"ask": None, "ask_depth": ()})
        sig = signal.model_copy(update={"quote": empty})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.NO_BOOK_LIQUIDITY)

    def test_zero_depth_blocks_even_with_a_price(self, engine, signal, market, snapshot):
        no_depth = signal.quote.model_copy(update={"ask_depth": ()})
        sig = signal.model_copy(update={"quote": no_depth})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.NO_BOOK_LIQUIDITY)


class TestTimeToClose:
    def test_no_lottery_tickets(self, engine, signal, market, snapshot):
        closing = market.model_copy(update={"close_time": NOW + timedelta(seconds=60)})
        assert_rejected(evaluate(engine, signal, closing, snapshot), Limit.MIN_TIME_TO_CLOSE)

    def test_no_year_long_capital_lockup(self, engine, signal, market, snapshot):
        distant = market.model_copy(update={"close_time": NOW + timedelta(days=400)})
        assert_rejected(evaluate(engine, signal, distant, snapshot), Limit.MAX_TIME_TO_CLOSE)

    def test_closed_market_is_below_minimum(self, engine, signal, market, snapshot):
        past = market.model_copy(update={"close_time": NOW - timedelta(days=1)})
        assert_rejected(evaluate(engine, signal, past, snapshot), Limit.MIN_TIME_TO_CLOSE)


class TestOrderType:
    def test_market_order_requires_config_and_signal_flag(self, engine, signal, market, snapshot):
        sig = signal.model_copy(update={"allow_market_order": True})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.MARKET_ORDER_NOT_PERMITTED)


class TestSlippage:
    def test_limit_far_through_the_touch_is_rejected(self, config, signal, market, snapshot):
        tight = config.model_copy(update={"max_slippage_bps": 10})
        engine = RiskEngine(
            tight,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        # Ask is 0.40; a 0.50 limit crosses 1000bps through it.
        sig = signal.model_copy(update={"limit_price": Probability("0.50")})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.MAX_SLIPPAGE_BPS)

    def test_passive_limit_below_the_touch_is_not_slippage(self, engine, signal, market, snapshot):
        # Resting inside the spread does not cross, so it cannot slip.
        sig = signal.model_copy(update={"limit_price": Probability("0.30")})
        assert evaluate(engine, sig, market, snapshot).outcome is DecisionOutcome.APPROVED


class TestOrderRateLimits:
    def test_per_minute_cap(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"orders_last_minute": 10})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_ORDERS_PER_MINUTE)

    def test_per_day_cap(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"orders_today": 100})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_ORDERS_PER_DAY)


class TestSizingOutcomes:
    def test_no_edge_is_rejected(self, engine, signal, market, snapshot):
        # thesis == limit: Kelly is zero.
        sig = signal.model_copy(update={"thesis_price": Probability("0.40")})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.NO_EDGE)

    def test_size_rounding_to_zero_is_rejected_not_rounded_up(
        self, engine, signal, market, snapshot
    ):
        # A bankroll too small to buy one contract must reject, never round up to 1.
        broke = snapshot.model_copy(
            update={"equity": Usd("0.10"), "cash": Usd("0.10"), "peak_equity": Usd("0.10")}
        )
        assert_rejected(evaluate(engine, signal, market, broke), Limit.SIZE_ROUNDS_TO_ZERO)


class TestBookDepth:
    def test_order_is_capped_to_a_fraction_of_visible_depth(
        self, engine, signal, market, snapshot
    ):
        thin = signal.quote.model_copy(
            update={"ask_depth": (DepthLevel(price=Probability("0.40"), quantity=40),)}
        )
        sig = signal.model_copy(update={"quote": thin})
        decision = evaluate(engine, sig, market, snapshot)
        assert decision.outcome is DecisionOutcome.APPROVED
        assert decision.order is not None
        # 50% of 40 visible contracts, not the 100 the signal asked for.
        assert decision.order.quantity == 20

    def test_book_too_thin_for_any_order(self, engine, signal, market, snapshot):
        one = signal.quote.model_copy(
            update={"ask_depth": (DepthLevel(price=Probability("0.40"), quantity=1),)}
        )
        sig = signal.model_copy(update={"quote": one})
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.MAX_ORDER_SIZE_PCT_OF_BOOK)


class TestCapitalLimits:
    def test_max_position_size(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(
            update={"deployed_by_outcome": {signal.outcome.outcome_key: Usd("990")}}
        )
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_POSITION_SIZE)

    def test_max_position_pct_of_bankroll(self, config, signal, market, snapshot):
        tight = config.model_copy(update={"max_position_pct_of_bankroll": Decimal("0.001")})
        engine = RiskEngine(
            tight,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        # $10 cap at 0.001 * $10,000 equity; 100 contracts at 0.40 is $40. Sizing caps
        # it to 25 contracts, so the *check* only fires with an existing position.
        snap = snapshot.model_copy(
            update={"deployed_by_outcome": {signal.outcome.outcome_key: Usd("9")}}
        )
        assert_rejected(
            evaluate(engine, signal, market, snap), Limit.MAX_POSITION_PCT_OF_BANKROLL
        )

    def test_max_total_deployed(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"deployed_total": Usd("9990")})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_TOTAL_DEPLOYED)

    def test_max_venue_deployed(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"deployed_by_venue": {Venue.KALSHI: Usd("7990")}})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_VENUE_DEPLOYED)

    def test_max_daily_new_capital(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"day_new_capital": Usd("4990")})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_DAILY_NEW_CAPITAL)

    def test_min_cash_reserve(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(update={"cash": Usd("120")})
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MIN_CASH_RESERVE)


class TestExposureLimits:
    def test_single_event_exposure(self, engine, signal, market, snapshot):
        snap = snapshot.model_copy(
            update={"deployed_by_event": {signal.outcome.event_key: Usd("1990")}}
        )
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_SINGLE_EVENT_EXPOSURE)

    def test_ten_markets_on_one_event_are_one_bet(self, engine, signal, market, snapshot):
        """The limit aggregates by correlation tag, not by market. This is the check
        that stops ten 'Fed cuts in March' markets from passing as ten small positions."""
        snap = snapshot.model_copy(
            update={"deployed_by_correlation_tag": {"fed-september-2026": Usd("1990")}}
        )
        assert_rejected(evaluate(engine, signal, market, snap), Limit.MAX_CORRELATED_EXPOSURE)

    def test_untagged_market_is_correlated_with_its_whole_venue(
        self, engine, signal, market, snapshot
    ):
        untagged = market.model_copy(update={"correlation_tags": ()})
        snap = snapshot.model_copy(
            update={"deployed_by_correlation_tag": {"untagged:kalshi": Usd("1990")}}
        )
        assert_rejected(evaluate(engine, signal, untagged, snap), Limit.MAX_CORRELATED_EXPOSURE)

    def test_illiquid_market_exposure_capped(self, engine, signal, market, snapshot):
        illiquid = market.model_copy(update={"volume_24h": Usd("100")})
        snap = snapshot.model_copy(update={"deployed_illiquid": Usd("2490")})
        assert_rejected(evaluate(engine, signal, illiquid, snap), Limit.MAX_ILLIQUID_PCT)

    def test_unknown_volume_counts_as_illiquid(self, engine, signal, market, snapshot):
        """Absence of evidence about depth is not evidence of depth."""
        unknown = market.model_copy(update={"volume_24h": None})
        snap = snapshot.model_copy(update={"deployed_illiquid": Usd("2490")})
        assert_rejected(evaluate(engine, signal, unknown, snap), Limit.MAX_ILLIQUID_PCT)


class TestEdgeAfterFees:
    def test_thin_edge_dies_to_fees(self, engine, signal, market, snapshot):
        # 1 probability point of edge against a fee of ~1.7 cents/contract.
        sig = signal.model_copy(
            update={"thesis_price": Probability("0.405"), "limit_price": Probability("0.40")}
        )
        assert_rejected(evaluate(engine, sig, market, snapshot), Limit.MIN_EDGE_BPS_AFTER_FEES)

    def test_fee_safety_multiplier_can_kill_a_marginal_trade(
        self, config, signal, market, snapshot
    ):
        """The unverified-fee safety margin must actually bite, or it is decoration."""
        punitive = make_fee_model_config().model_copy(
            update={"safety_multiplier": Decimal("50")}
        )
        engine = RiskEngine(
            config, fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, punitive)}
        )
        assert_rejected(evaluate(engine, signal, market, snapshot), Limit.MIN_EDGE_BPS_AFTER_FEES)


class TestApprovalTier:
    def test_large_order_goes_to_pending_approval(self, config, signal, market, snapshot):
        engine = RiskEngine(
            config.model_copy(update={"auto_approve_threshold": Usd("10")}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        decision = evaluate(engine, signal, market, snapshot)
        assert decision.outcome is DecisionOutcome.PENDING_APPROVAL
        assert decision.expires_at == NOW + timedelta(seconds=300)
        assert decision.quoted_price == Probability("0.40")

    def test_expired_approval_is_a_rejected_approval(self, config, signal, market, snapshot, quote):
        engine = RiskEngine(
            config.model_copy(update={"auto_approve_threshold": Usd("10")}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        pending = evaluate(engine, signal, market, snapshot)
        later = engine.validate_approval(pending, quote, now=NOW + timedelta(seconds=600))
        assert_rejected(later, Limit.APPROVAL_EXPIRED)

    def test_approval_on_a_moved_price_is_void(self, config, signal, market, snapshot, quote):
        engine = RiskEngine(
            config.model_copy(update={"auto_approve_threshold": Usd("10")}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        pending = evaluate(engine, signal, market, snapshot)
        moved = quote.model_copy(
            update={
                "bid": Probability("0.48"),
                "ask": Probability("0.50"),
                "ask_depth": (DepthLevel(price=Probability("0.50"), quantity=5000),),
            }
        )
        assert_rejected(
            engine.validate_approval(pending, moved, now=NOW + timedelta(seconds=60)),
            Limit.APPROVAL_PRICE_MOVED,
        )

    def test_timely_approval_at_an_unchanged_price_executes(
        self, config, signal, market, snapshot, quote
    ):
        engine = RiskEngine(
            config.model_copy(update={"auto_approve_threshold": Usd("10")}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        pending = evaluate(engine, signal, market, snapshot)
        approved = engine.validate_approval(pending, quote, now=NOW + timedelta(seconds=60))
        assert approved.outcome is DecisionOutcome.APPROVED
        assert approved.order == pending.order

    def test_validate_approval_on_a_non_pending_decision_fails_closed(
        self, engine, signal, market, snapshot, quote
    ):
        approved = evaluate(engine, signal, market, snapshot)
        with pytest.raises(RiskEngineFailure):
            engine.validate_approval(approved, quote)

    def test_approval_with_no_liquidity_at_execution_time(
        self, config, signal, market, snapshot, quote
    ):
        engine = RiskEngine(
            config.model_copy(update={"auto_approve_threshold": Usd("10")}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
        )
        pending = evaluate(engine, signal, market, snapshot)
        empty = quote.model_copy(update={"ask": None, "ask_depth": ()})
        assert_rejected(
            engine.validate_approval(pending, empty, now=NOW + timedelta(seconds=10)),
            Limit.NO_BOOK_LIQUIDITY,
        )


class TestFailClosed:
    def test_mismatched_market_raises_rather_than_guessing(
        self, engine, signal, market, snapshot
    ):
        other = market.model_copy(update={"market_key": "SOMETHING-ELSE"})
        with pytest.raises(RiskEngineFailure, match="references"):
            evaluate(engine, signal, other, snapshot)

    def test_internal_exception_becomes_a_failure_not_an_order(
        self, engine, signal, market, snapshot, monkeypatch
    ):
        def explode(*args, **kwargs):
            raise ValueError("simulated fault deep in sizing")

        monkeypatch.setattr("pmx.risk.engine.size_position", explode)
        with pytest.raises(RiskEngineFailure, match="simulated fault"):
            evaluate(engine, signal, market, snapshot)

    def test_failure_is_recorded_as_a_halt_in_the_ledger(
        self, config, signal, market, snapshot, ledger, monkeypatch
    ):
        engine = RiskEngine(
            config,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
            ledger=ledger,
        )

        def explode(*args, **kwargs):
            raise ValueError("boom")

        monkeypatch.setattr("pmx.risk.engine.size_position", explode)
        with pytest.raises(RiskEngineFailure):
            evaluate(engine, signal, market, snapshot)
        halts = ledger.entries(kind=EntryKind.HALT)
        assert len(halts) == 1
        assert halts[0].payload["reason"] == "risk_engine_exception"


class TestIdempotency:
    def test_same_trade_produces_the_same_key(self, engine, signal, market, snapshot):
        first = evaluate(engine, signal, market, snapshot)
        second = evaluate(engine, signal, market, snapshot)
        assert first.order is not None and second.order is not None
        assert first.order.idempotency_key == second.order.idempotency_key

    def test_different_price_produces_a_different_key(self, engine, signal, market, snapshot):
        first = evaluate(engine, signal, market, snapshot)
        changed = signal.model_copy(update={"limit_price": Probability("0.39")})
        second = evaluate(engine, changed, market, snapshot)
        assert first.order is not None and second.order is not None
        assert first.order.idempotency_key != second.order.idempotency_key

    def test_different_size_produces_a_different_key(self, engine, signal, market, snapshot):
        first = evaluate(engine, signal, market, snapshot)
        smaller = signal.model_copy(update={"max_quantity": 50})
        second = evaluate(engine, smaller, market, snapshot)
        assert first.order is not None and second.order is not None
        assert first.order.idempotency_key != second.order.idempotency_key


class TestAuditTrail:
    def test_every_rejection_names_its_limit_in_the_ledger(
        self, config, signal, market, snapshot, ledger
    ):
        engine = RiskEngine(
            config,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
            ledger=ledger,
        )
        snap = snapshot.model_copy(update={"deployed_total": Usd("9990")})
        evaluate(engine, signal, market, snap)
        entries = ledger.entries(kind=EntryKind.SIGNAL_REJECTED)
        assert len(entries) == 1
        assert entries[0].payload["limit"] == str(Limit.MAX_TOTAL_DEPLOYED)
        assert "9990" in str(entries[0].payload["values"])

    def test_approved_order_is_recorded(self, config, signal, market, snapshot, ledger):
        engine = RiskEngine(
            config,
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
            ledger=ledger,
        )
        evaluate(engine, signal, market, snapshot)
        entries = ledger.entries(kind=EntryKind.ORDER_PROPOSED)
        assert len(entries) == 1
        assert entries[0].payload["quantity"] == 100

    def test_pending_approval_is_recorded(self, config, signal, market, snapshot, ledger):
        engine = RiskEngine(
            config.model_copy(update={"auto_approve_threshold": Usd("10")}),
            fee_models={str(Venue.KALSHI): FeeModel(Venue.KALSHI, make_fee_model_config())},
            ledger=ledger,
        )
        evaluate(engine, signal, market, snapshot)
        assert len(ledger.entries(kind=EntryKind.APPROVAL_REQUESTED)) == 1


class TestSellSide:
    def test_sell_signal_is_evaluated_symmetrically(self, engine, market, snapshot, outcome, quote):
        sig = Signal(
            signal_id="sig-sell",
            strategy="manual",
            outcome=outcome,
            side=Side.SELL,
            thesis_price=Probability("0.20"),
            limit_price=Probability("0.38"),
            max_quantity=100,
            rationale="Sell side test.",
            created_at=NOW,
            quote=quote,
        )
        decision = evaluate(engine, sig, market, snapshot)
        assert decision.outcome is DecisionOutcome.APPROVED
        assert decision.order is not None
        assert decision.order.side is Side.SELL


class TestShortRisk:
    """Regression: exposure limits must be computed on maximum loss, not on proceeds.

    Selling a contract at 0.10 collects $0.10 and can lose $0.90 — settlement pays the
    holder $1 and we owe it. Booking the short at its proceeds understated risk by
    (1-p)/p, a 9x error at 10 cents, on precisely the leg of the book where a strategy
    is most tempted to sell.
    """

    def _short_signal(self, outcome, quote, price: str, thesis: str, quantity: int = 100):
        return Signal(
            signal_id="sig-short",
            strategy="manual",
            outcome=outcome,
            side=Side.SELL,
            thesis_price=Probability(thesis),
            limit_price=Probability(price),
            max_quantity=quantity,
            rationale="Short risk regression.",
            created_at=NOW,
            quote=quote,
        )

    @pytest.fixture
    def cheap_book(self, outcome):
        return Quote(
            outcome=outcome,
            bid=Probability("0.10"),
            ask=Probability("0.12"),
            bid_depth=(DepthLevel(price=Probability("0.10"), quantity=5000),),
            ask_depth=(DepthLevel(price=Probability("0.12"), quantity=5000),),
            observed_at=NOW,
            source=QuoteSource.BOOK,
        )

    def test_short_is_booked_at_maximum_loss_not_proceeds(
        self, engine, market, snapshot, outcome, cheap_book
    ):
        sig = self._short_signal(outcome, cheap_book, price="0.10", thesis="0.02")
        decision = evaluate(engine, sig, market, snapshot)
        assert decision.outcome is DecisionOutcome.APPROVED
        assert decision.order is not None
        # 100 contracts sold at 0.10 risk $90, not $10.
        assert decision.order.max_cost >= Usd("90")

    def test_short_breaches_the_position_cap_that_proceeds_would_have_cleared(
        self, engine, market, snapshot, outcome, cheap_book
    ):
        # $920 of existing exposure plus $90 of new risk breaches the $1000 cap.
        # Booked at proceeds ($10) it would have passed with room to spare.
        snap = snapshot.model_copy(
            update={"deployed_by_outcome": {outcome.outcome_key: Usd("920")}}
        )
        sig = self._short_signal(outcome, cheap_book, price="0.10", thesis="0.02")
        assert_rejected(evaluate(engine, sig, market, snap), Limit.MAX_POSITION_SIZE)

    def test_short_sizing_divides_by_risk_not_by_price(self, outcome, cheap_book):
        from pmx.risk.sizing import size_position

        sizing = size_position(
            thesis=Probability("0.02"),
            price=Probability("0.10"),
            side=Side.SELL,
            bankroll=Usd("900"),
            kelly_multiplier=Decimal("1"),
            max_position_value=Usd("1000000"),
            max_position_pct_of_bankroll=Decimal("1"),
            signal_max_quantity=1_000_000,
        )
        # Full Kelly here is (0.10 - 0.02) / 0.10 = 0.8, so $720 of risk budget.
        # At $0.90 of risk per contract that is 800 contracts, not the 7200 you get
        # by dividing by the $0.10 price.
        assert sizing.quantity == 800

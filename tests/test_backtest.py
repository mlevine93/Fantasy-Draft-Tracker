"""Backtest harness and promotion gate.

The backtest tests are all about pessimism: the harness must fill at the far side, refuse
liquidity that was not there, charge the real fee, and never see the future. A harness
that is wrong in the optimistic direction does not produce a bad estimate — it produces a
strategy that passes its gate and loses money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pmx.audit.ledger import EntryKind
from pmx.core.models import (
    DepthLevel,
    OutcomeRef,
    PromotionState,
    Quote,
    QuoteSource,
    Side,
    Signal,
    Venue,
)
from pmx.core.money import Probability, Usd
from pmx.data.backtest import run_backtest
from pmx.strategies.promotion import PromotionError, PromotionStore
from pmx.venues.fees import FeeModel
from tests.conftest import make_fee_model_config

START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
OUTCOME = OutcomeRef(
    venue=Venue.KALSHI, market_key="FED", outcome_key="FED:YES", event_key="FED-EVT"
)


def tick(minute: int, bid: str, ask: str, depth: int = 1000) -> Quote:
    return Quote(
        outcome=OUTCOME,
        bid=Probability(bid),
        ask=Probability(ask),
        bid_depth=(DepthLevel(price=Probability(bid), quantity=depth),),
        ask_depth=(DepthLevel(price=Probability(ask), quantity=depth),),
        observed_at=START + timedelta(minutes=minute),
        source=QuoteSource.BOOK,
    )


def signal_at(quote: Quote, *, limit: str, thesis: str, quantity: int = 100) -> Signal:
    return Signal(
        signal_id=f"sig-{quote.observed_at.isoformat()}",
        strategy="test",
        outcome=OUTCOME,
        side=Side.BUY,
        thesis_price=Probability(thesis),
        limit_price=Probability(limit),
        max_quantity=quantity,
        rationale="backtest",
        created_at=quote.observed_at,
        quote=quote,
    )


@pytest.fixture
def fees() -> FeeModel:
    return FeeModel(Venue.KALSHI, make_fee_model_config())


def once(target_minute: int, **signal_kwargs):
    """A strategy that fires a single signal on one tick."""

    def strategy(quote: Quote) -> list[Signal]:
        if quote.observed_at == START + timedelta(minutes=target_minute):
            return [signal_at(quote, **signal_kwargs)]
        return []

    return strategy


class TestFillRealism:
    def test_buy_pays_the_ask_not_the_mid(self, fees) -> None:
        """Filling at the mid earns half the spread for free on every trade, which is
        usually the whole reported edge."""
        ticks = [tick(0, "0.38", "0.42"), tick(1, "0.38", "0.42")]
        result = run_backtest(ticks, once(0, limit="0.45", thesis="0.60"), fees)
        assert result.trades[0].fill_price == Probability("0.42")

    def test_size_is_capped_by_visible_depth(self, fees) -> None:
        """Nothing fills against liquidity that was not on the book."""
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40", depth=40)]
        result = run_backtest(
            ticks, once(0, limit="0.45", thesis="0.60", quantity=1000), fees,
            max_pct_of_book=Decimal("0.5"),
        )
        assert result.trades[0].filled_quantity == 20
        assert not result.trades[0].fully_filled

    def test_a_limit_that_never_trades_does_not_fill(self, fees) -> None:
        ticks = [tick(0, "0.38", "0.42"), tick(1, "0.39", "0.43")]
        result = run_backtest(ticks, once(0, limit="0.20", thesis="0.60"), fees)
        assert result.trades == ()
        assert result.signals_unfilled == 1

    def test_fees_are_charged_on_every_fill(self, fees) -> None:
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40")]
        result = run_backtest(ticks, once(0, limit="0.45", thesis="0.60"), fees)
        assert result.fees_paid > Usd.zero()
        assert result.net_pnl == result.gross_pnl - result.fees_paid

    def test_no_look_ahead(self, fees) -> None:
        """The fill may only come from ticks strictly after the signal. If look-ahead
        were possible, the signal on the last tick would fill against itself."""
        ticks = [tick(0, "0.38", "0.40")]
        result = run_backtest(ticks, once(0, limit="0.45", thesis="0.60"), fees)
        assert result.trades == ()
        assert result.signals_unfilled == 1

    def test_empty_book_does_not_fill(self, fees) -> None:
        empty = Quote(
            outcome=OUTCOME,
            bid=None,
            ask=None,
            observed_at=START + timedelta(minutes=1),
            source=QuoteSource.BOOK,
        )
        result = run_backtest(
            [tick(0, "0.38", "0.40"), empty], once(0, limit="0.45", thesis="0.60"), fees
        )
        assert result.trades == ()


class TestSettlement:
    def test_marks_to_last_price_when_no_resolutions_supplied(self, fees) -> None:
        """Recorded ticks hold prices, not outcomes. Inventing settlement would be the
        fabricated-data failure the whole system is built to avoid."""
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40"), tick(2, "0.58", "0.60")]
        result = run_backtest(ticks, once(0, limit="0.45", thesis="0.60"), fees)
        assert result.marked_to_market
        # Bought at 0.40, closed at the 0.58 bid.
        assert result.trades[0].realised_edge == Decimal("0.18")

    def test_settles_at_truth_when_resolutions_are_supplied(self, fees) -> None:
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40")]
        result = run_backtest(
            ticks, once(0, limit="0.45", thesis="0.60"), fees, resolutions={"FED:YES": 1}
        )
        assert not result.marked_to_market
        # Bought 100 at 0.40, settled at 1.00: $60 gross.
        assert result.gross_pnl == Usd("60")

    def test_a_losing_resolution_is_a_full_loss(self, fees) -> None:
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40")]
        result = run_backtest(
            ticks, once(0, limit="0.45", thesis="0.60"), fees, resolutions={"FED:YES": 0}
        )
        assert result.gross_pnl == Usd("-40")


class TestMetrics:
    def _many(self, fees, pnl_pattern: list[tuple[str, str]]):
        """Build one trade per (entry ask, exit bid) pair, each on its own outcome."""
        results = []
        for index, (entry, exit_price) in enumerate(pnl_pattern):
            ticks = [
                tick(index * 10, "0.01", entry),
                tick(index * 10 + 1, "0.01", entry),
                tick(index * 10 + 2, exit_price, "0.99"),
            ]
            results.append(
                run_backtest(ticks, once(index * 10, limit="0.99", thesis="0.99"), fees)
            )
        return results

    def test_hit_rate_counts_profitable_trades(self, fees) -> None:
        wins = self._many(fees, [("0.40", "0.60"), ("0.40", "0.20")])
        assert wins[0].hit_rate == Decimal(1)
        assert wins[1].hit_rate == Decimal(0)

    def test_pnl_excluding_top_five_is_reported(self, fees) -> None:
        """§6: if the strategy dies without its best five trades, it is a lottery."""
        ticks: list[Quote] = []
        for index in range(8):
            ticks.extend(
                [
                    tick(index * 10, "0.01", "0.40"),
                    tick(index * 10 + 1, "0.01", "0.40"),
                    tick(index * 10 + 2, "0.60", "0.99"),
                ]
            )

        def strategy(quote: Quote) -> list[Signal]:
            minute = int((quote.observed_at - START).total_seconds() // 60)
            if minute % 10 == 0:
                return [signal_at(quote, limit="0.99", thesis="0.99", quantity=10)]
            return []

        result = run_backtest(ticks, strategy, fees)
        assert len(result.trades) >= 6
        assert result.net_pnl_excluding_top_5 < result.net_pnl

    def test_edge_shortfall_exposes_overconfidence(self, fees) -> None:
        """The most useful number in the report: does the strategy know what it thinks
        it knows?"""
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40"), tick(2, "0.41", "0.43")]
        result = run_backtest(ticks, once(0, limit="0.40", thesis="0.90"), fees)
        # Predicted a 50-point edge, realised one point.
        assert result.average_predicted_edge == Decimal("0.5")
        assert result.edge_shortfall > Decimal("0.4")

    def test_max_drawdown_uses_trade_order_not_sorted_order(self, fees) -> None:
        """Settled explicitly, because mark-to-market exits every open trade at the same
        final price and so cannot produce a drawdown at all — a property of the harness
        worth knowing before reading any equity curve it produces."""
        outcomes = [
            OutcomeRef(venue=Venue.KALSHI, market_key=f"M{i}", outcome_key=f"M{i}:YES",
                       event_key=f"E{i}")
            for i in range(3)
        ]
        ticks: list[Quote] = []
        for index, outcome in enumerate(outcomes):
            for offset in (0, 1):
                ticks.append(
                    Quote(
                        outcome=outcome,
                        bid=Probability("0.01"),
                        ask=Probability("0.40"),
                        bid_depth=(DepthLevel(price=Probability("0.01"), quantity=1000),),
                        ask_depth=(DepthLevel(price=Probability("0.40"), quantity=1000),),
                        observed_at=START + timedelta(minutes=index * 10 + offset),
                        source=QuoteSource.BOOK,
                    )
                )

        def strategy(quote: Quote) -> list[Signal]:
            minute = int((quote.observed_at - START).total_seconds() // 60)
            if minute % 10 != 0:
                return []
            return [
                Signal(
                    signal_id=f"sig-{quote.outcome.outcome_key}",
                    strategy="test",
                    outcome=quote.outcome,
                    side=Side.BUY,
                    thesis_price=Probability("0.99"),
                    limit_price=Probability("0.99"),
                    max_quantity=10,
                    rationale="drawdown test",
                    created_at=quote.observed_at,
                    quote=quote,
                )
            ]

        # Win, then lose, then win: the loss in the middle is the drawdown.
        result = run_backtest(
            ticks,
            strategy,
            fees,
            resolutions={"M0:YES": 1, "M1:YES": 0, "M2:YES": 1},
        )
        assert len(result.trades) == 3
        assert result.max_drawdown > Usd.zero()
        assert result.hit_rate == Decimal(2) / Decimal(3)

    def test_no_trades_yields_a_clean_empty_result(self, fees) -> None:
        result = run_backtest([tick(0, "0.38", "0.40")], lambda _q: [], fees)
        assert result.trades == ()
        assert result.net_pnl == Usd.zero()
        assert result.sharpe is None

    def test_summary_flags_mark_to_market(self, fees) -> None:
        ticks = [tick(0, "0.38", "0.40"), tick(1, "0.38", "0.40")]
        result = run_backtest(ticks, once(0, limit="0.45", thesis="0.60"), fees)
        assert "no settlement data" in result.summary()


class TestPromotionGate:
    def test_unregistered_strategy_is_not_live(self, tmp_path) -> None:
        with PromotionStore(tmp_path / "promotions.db") as store:
            assert store.state_of("never-heard-of-it") is PromotionState.BACKTEST

    def test_cannot_jump_from_backtest_to_live(self, tmp_path) -> None:
        """§6 requires paper trading against live data in between, and skipping it is
        exactly the shortcut a deadline tempts you into."""
        with (
            PromotionStore(tmp_path / "p.db") as store,
            pytest.raises(PromotionError, match="paper trading"),
        ):
            store.set_state(
                "calibration", PromotionState.LIVE, operator="mack", note="looks good"
            )

    def test_the_supported_path_to_live_works(self, tmp_path, ledger) -> None:
        with PromotionStore(tmp_path / "p.db") as store:
            store.set_state(
                "calibration", PromotionState.PAPER, operator="mack", note="backtest passed"
            )
            store.set_state(
                "calibration",
                PromotionState.LIVE,
                operator="mack",
                note="two weeks paper, matches backtest",
                ledger=ledger,
            )
            assert store.state_of("calibration") is PromotionState.LIVE

        entries = ledger.entries(kind=EntryKind.CONFIG_CHANGED)
        assert entries[-1].payload["to"] == "live"
        assert entries[-1].payload["operator"] == "mack"

    def test_promotion_requires_an_operator_and_a_reason(self, tmp_path) -> None:
        with PromotionStore(tmp_path / "p.db") as store:
            with pytest.raises(PromotionError, match="operator"):
                store.set_state("s", PromotionState.PAPER, operator=" ", note="x")
            with pytest.raises(PromotionError, match="justification"):
                store.set_state("s", PromotionState.PAPER, operator="mack", note="")

    def test_disabling_needs_no_human(self, tmp_path, ledger) -> None:
        """Demotion is the safe direction. A system that needed a human to disable a
        losing strategy would keep trading while it waited."""
        with PromotionStore(tmp_path / "p.db") as store:
            store.set_state("s", PromotionState.PAPER, operator="mack", note="ok")
            store.set_state("s", PromotionState.LIVE, operator="mack", note="promoted")
            store.disable("s", reason="per_strategy_loss_halt tripped", ledger=ledger)
            assert store.state_of("s") is PromotionState.DISABLED

    def test_all_states_feeds_the_risk_engine_snapshot(self, tmp_path) -> None:
        with PromotionStore(tmp_path / "p.db") as store:
            store.set_state("a", PromotionState.PAPER, operator="m", note="n")
            store.set_state("b", PromotionState.SHADOW, operator="m", note="n")
            store.set_state("a", PromotionState.SHADOW, operator="m", note="n")
            assert store.all_states() == {
                "a": PromotionState.SHADOW,
                "b": PromotionState.SHADOW,
            }

    def test_history_is_append_only(self, tmp_path) -> None:
        with PromotionStore(tmp_path / "p.db") as store:
            store.set_state("s", PromotionState.PAPER, operator="m", note="first")
            store.set_state("s", PromotionState.SHADOW, operator="m", note="second")
            history = store.history("s")
            assert [record.note for record in history] == ["second", "first"]

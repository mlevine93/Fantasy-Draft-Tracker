"""Backtest harness (§6 gate 1).

The purpose of a backtest here is not to find out how much money a strategy makes. It is
to find out whether the strategy survives being modelled honestly — and the honest model
is pessimistic in every place where a choice exists:

* **Fills at the far side.** A buy pays the ask, never the mid. Backtests that fill at
  the mid earn half the spread on every trade for free, which is usually the entire
  reported edge.
* **Only liquidity that was actually visible.** Size is capped by the depth recorded at
  that instant. Nothing fills against liquidity that was not there.
* **Partial fills are real.** If the book held 40 and we wanted 100, we got 40.
* **The real fee function**, including the round-up to the cent that makes small orders
  disproportionately expensive.
* **No look-ahead.** The signal for time *t* may only see ticks at or before *t*, and the
  fill can only come from ticks strictly after it.

Two metrics do most of the work. **Average predicted edge vs realised edge** says whether
the strategy knows what it thinks it knows. **P&L excluding the top five trades** says
whether it has an edge or a lottery ticket — §6's words, and the right test.

## What this harness will not do

It will not invent settlement. Recorded ticks contain prices, not outcomes, so P&L is
marked to the last observed bid unless the caller supplies real resolutions. A backtest
that assumed how markets resolved would be the purest form of the fabricated-data
failure this whole system is built to avoid.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from pmx.core.models import Quote, Side, Signal
from pmx.core.money import Probability, Usd
from pmx.venues.fees import FeeModel

__all__ = ["BacktestResult", "SimulatedTrade", "run_backtest"]


class SimulatedTrade(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str
    outcome_key: str
    side: Side
    requested_quantity: int
    filled_quantity: int
    fill_price: Probability
    predicted_edge: Decimal
    realised_edge: Decimal
    fee: Usd
    pnl: Usd
    entered_at: datetime
    exited_at: datetime

    @property
    def fully_filled(self) -> bool:
        return self.filled_quantity == self.requested_quantity


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trades: tuple[SimulatedTrade, ...]
    signals_generated: int
    signals_unfilled: int
    gross_pnl: Usd
    fees_paid: Usd
    net_pnl: Usd
    #: §6: "if the strategy dies without them, it's a lottery, not an edge".
    net_pnl_excluding_top_5: Usd
    hit_rate: Decimal
    max_drawdown: Usd
    sharpe: Decimal | None
    average_predicted_edge: Decimal
    average_realised_edge: Decimal
    #: True when P&L was marked to the last observed price rather than to settlement.
    marked_to_market: bool

    @property
    def edge_shortfall(self) -> Decimal:
        """How much of the predicted edge failed to materialise.

        Consistently positive means the strategy is systematically overconfident, which
        is far more useful to know than the P&L number itself.
        """
        return self.average_predicted_edge - self.average_realised_edge

    def summary(self) -> str:
        lines = [
            f"trades              {len(self.trades)} ({self.signals_unfilled} signals unfilled)",
            f"net P&L             {self.net_pnl}  (gross {self.gross_pnl}, fees {self.fees_paid})",
            f"excl. top 5 trades  {self.net_pnl_excluding_top_5}",
            f"hit rate            {self.hit_rate:.1%}",
            f"max drawdown        {self.max_drawdown}",
            f"sharpe (per trade)  {self.sharpe if self.sharpe is not None else 'n/a'}",
            f"predicted edge      {self.average_predicted_edge:.4f}",
            f"realised edge       {self.average_realised_edge:.4f}",
            f"edge shortfall      {self.edge_shortfall:.4f}",
        ]
        if self.marked_to_market:
            lines.append("NOTE: marked to last observed price — no settlement data supplied")
        return "\n".join(lines)


def run_backtest(
    ticks: Sequence[Quote],
    strategy_fn: Callable[[Quote], list[Signal]],
    fee_model: FeeModel,
    *,
    resolutions: Mapping[str, int] | None = None,
    max_pct_of_book: Decimal = Decimal("0.25"),
) -> BacktestResult:
    """Replay `ticks` in time order, filling signals against later ticks only.

    `resolutions` maps outcome key to 1 (resolved yes) or 0 (resolved no). Supply it to
    settle at truth; omit it to mark to the last observed price, which is reported.
    """
    ordered = sorted(ticks, key=lambda quote: quote.observed_at)
    trades: list[SimulatedTrade] = []
    generated = 0
    unfilled = 0

    for index, tick in enumerate(ordered):
        signals = strategy_fn(tick)
        generated += len(signals)

        for signal in signals:
            # Fills may only come from ticks strictly after the one the signal saw.
            future = ordered[index + 1 :]
            trade = _simulate(
                signal,
                future,
                fee_model,
                resolutions=resolutions,
                max_pct_of_book=max_pct_of_book,
            )
            if trade is None:
                unfilled += 1
            else:
                trades.append(trade)

    return _summarise(tuple(trades), generated, unfilled, marked_to_market=resolutions is None)


def _simulate(
    signal: Signal,
    future: Sequence[Quote],
    fee_model: FeeModel,
    *,
    resolutions: Mapping[str, int] | None,
    max_pct_of_book: Decimal,
) -> SimulatedTrade | None:
    """Try to fill one signal against subsequent ticks."""
    for tick in future:
        if tick.outcome.outcome_key != signal.outcome.outcome_key:
            continue

        touch = tick.touch(signal.side)
        if touch is None:
            continue

        # A limit order only trades when the market comes to it. Buying needs an ask at
        # or below our limit; selling needs a bid at or above it.
        if signal.side is Side.BUY and touch > signal.limit_price:
            continue
        if signal.side is Side.SELL and touch < signal.limit_price:
            continue

        visible = tick.visible_quantity(signal.side)
        allowed = int((Decimal(visible) * max_pct_of_book).to_integral_value())
        filled = min(signal.max_quantity, allowed)
        if filled <= 0:
            continue

        # The far side, not the mid. This single choice is the difference between a
        # backtest and a sales pitch.
        fill_price = touch
        fee = fee_model.estimate(fill_price, filled, is_maker=False)
        exit_price, exited_at = _exit_price(signal, future, resolutions)

        gross = _gross_pnl(signal.side, fill_price, exit_price, filled)
        predicted = signal.raw_edge()
        realised = (
            exit_price.value - fill_price.value
            if signal.side is Side.BUY
            else fill_price.value - exit_price.value
        )

        return SimulatedTrade(
            signal_id=signal.signal_id,
            outcome_key=signal.outcome.outcome_key,
            side=signal.side,
            requested_quantity=signal.max_quantity,
            filled_quantity=filled,
            fill_price=fill_price,
            predicted_edge=predicted,
            realised_edge=realised,
            fee=fee,
            pnl=gross - fee,
            entered_at=tick.observed_at,
            exited_at=exited_at,
        )

    return None


def _exit_price(
    signal: Signal, future: Sequence[Quote], resolutions: Mapping[str, int] | None
) -> tuple[Probability, datetime]:
    """Settlement if we have it, otherwise the last observed price on this outcome."""
    relevant = [
        tick for tick in future if tick.outcome.outcome_key == signal.outcome.outcome_key
    ]
    last = relevant[-1] if relevant else None

    if resolutions is not None and signal.outcome.outcome_key in resolutions:
        settled = Probability(resolutions[signal.outcome.outcome_key])
        return settled, last.observed_at if last else signal.created_at

    if last is None:
        return signal.limit_price, signal.created_at

    # Exit at the far side too: closing a long means hitting the bid.
    closing_side = Side.SELL if signal.side is Side.BUY else Side.BUY
    exit_touch = last.touch(closing_side)
    return exit_touch or signal.limit_price, last.observed_at


def _gross_pnl(side: Side, entry: Probability, exit_price: Probability, quantity: int) -> Usd:
    move = (
        exit_price.value - entry.value if side is Side.BUY else entry.value - exit_price.value
    )
    return Usd(move * quantity)


def _summarise(
    trades: tuple[SimulatedTrade, ...],
    generated: int,
    unfilled: int,
    *,
    marked_to_market: bool,
) -> BacktestResult:
    if not trades:
        return BacktestResult(
            trades=(),
            signals_generated=generated,
            signals_unfilled=unfilled,
            gross_pnl=Usd.zero(),
            fees_paid=Usd.zero(),
            net_pnl=Usd.zero(),
            net_pnl_excluding_top_5=Usd.zero(),
            hit_rate=Decimal(0),
            max_drawdown=Usd.zero(),
            sharpe=None,
            average_predicted_edge=Decimal(0),
            average_realised_edge=Decimal(0),
            marked_to_market=marked_to_market,
        )

    fees = Usd.zero()
    net = Usd.zero()
    for trade in trades:
        fees = fees + trade.fee
        net = net + trade.pnl
    gross = net + fees

    ranked = sorted(trades, key=lambda trade: trade.pnl.amount, reverse=True)
    excluding_top = Usd.zero()
    for trade in ranked[5:]:
        excluding_top = excluding_top + trade.pnl

    wins = sum(1 for trade in trades if trade.pnl > Usd.zero())
    hit_rate = Decimal(wins) / Decimal(len(trades))

    # Drawdown over the equity curve in trade order, not sorted order.
    equity = Decimal(0)
    peak = Decimal(0)
    worst = Decimal(0)
    for trade in sorted(trades, key=lambda trade: trade.entered_at):
        equity += trade.pnl.amount
        peak = max(peak, equity)
        worst = min(worst, equity - peak)

    returns = [trade.pnl.amount for trade in trades]
    sharpe: Decimal | None = None
    if len(returns) > 1:
        # statistics.pstdev on Decimals returns a Decimal; fmean would return a float and
        # reintroduce binary error into a reported number, so the mean is computed exactly.
        spread = statistics.pstdev(returns)
        if spread > 0:
            mean = sum(returns) / Decimal(len(returns))
            # Per-trade Sharpe, deliberately not annualised: annualising from a handful
            # of trades produces an impressive number with no information in it.
            sharpe = (mean / spread).quantize(Decimal("0.0001"))

    return BacktestResult(
        trades=trades,
        signals_generated=generated,
        signals_unfilled=unfilled,
        gross_pnl=gross,
        fees_paid=fees,
        net_pnl=net,
        net_pnl_excluding_top_5=excluding_top,
        hit_rate=hit_rate,
        max_drawdown=Usd(abs(worst)),
        sharpe=sharpe,
        average_predicted_edge=_mean(trade.predicted_edge for trade in trades),
        average_realised_edge=_mean(trade.realised_edge for trade in trades),
        marked_to_market=marked_to_market,
    )


def _mean(values: Iterable[Decimal]) -> Decimal:
    collected = list(values)
    if not collected:
        return Decimal(0)
    total = sum(collected, Decimal(0))
    return (total / Decimal(len(collected))).quantize(Decimal("0.000001"))


def quotes_from_recorded(rows: Sequence[Mapping[str, object]]) -> list[Quote]:
    """Rebuild canonical quotes from recorder output.

    Kept separate from `run_backtest` so that a schema correction to the recorder (the
    parsers are unverified — docs/api-notes.md §0) is a change in one place, and the
    archive can be reparsed without touching the simulation.
    """
    raise NotImplementedError(
        "reparsing recorded parquet into quotes lands with the first real recorded data; "
        "the schema it must read is still unverified"
    )

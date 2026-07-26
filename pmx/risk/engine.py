"""THE RISK ENGINE — the only path to an order.

Nothing else in this codebase may construct a `ProposedOrder`, and nothing else may call
a venue's order endpoint. `tests/test_single_order_path.py` enforces that by walking the
AST of the whole package; if a strategy or a script ever reaches a client directly, the
build fails rather than the money.

Three properties this module is built around:

**Fail closed.** Every unexpected exception becomes `RiskEngineFailure`, which the caller
must treat as a halt. There is no path through this module that returns an order after
something went wrong, and there is no bare `except`.

**Order of checks is deliberate.** State and data-quality checks run before any economics,
because a limit computed from a stale quote or divergent position state is not a limit —
it is arithmetic on fiction. Cheapest-and-most-fatal first.

**Every rejection names one limit.** `Rejection.limit` is a `Limit` enum member and
`Rejection.values` carries the numbers that produced it, so "why didn't we take that
trade" is answerable from the ledger alone, months later, in one line.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, model_validator

from pmx.audit.ledger import EntryKind, Ledger
from pmx.core.clock import utc_now
from pmx.core.models import (
    Market,
    PromotionState,
    ProposedOrder,
    Quote,
    QuoteSource,
    Side,
    Signal,
    TimeInForce,
)
from pmx.core.money import Probability, Usd, capital_at_risk
from pmx.risk.circuit import kill_switch_engaged
from pmx.risk.limits import Limit, Rejection, RiskConfig
from pmx.risk.sizing import size_position
from pmx.risk.state import PortfolioSnapshot
from pmx.venues.fees import FeeModel

__all__ = ["Decision", "DecisionOutcome", "RiskEngine", "RiskEngineFailure"]

BPS = Decimal(10_000)


class RiskEngineFailure(RuntimeError):
    """The engine could not reach a decision. The caller halts. Never retried blind."""


class DecisionOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    PENDING_APPROVAL = "pending_approval"


class Decision(BaseModel):
    """The engine's verdict.

    The validator enforces the shape of each outcome so that downstream code never has
    to ask "approved, but is there actually an order?". In particular a REJECTED
    decision cannot carry an order — which is the one combination that could lose money
    if some caller checked the wrong field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: DecisionOutcome
    signal_id: str
    order: ProposedOrder | None = None
    rejection: Rejection | None = None
    #: Set on PENDING_APPROVAL. Past this instant the approval is void, not merely old.
    expires_at: datetime | None = None
    #: Price the approval was requested against, for the drift check at execution time.
    quoted_price: Probability | None = None

    @model_validator(mode="after")
    def _shape_matches_outcome(self) -> Decision:
        if self.outcome is DecisionOutcome.REJECTED:
            if self.rejection is None:
                raise ValueError("a REJECTED decision must carry a rejection")
            if self.order is not None:
                raise ValueError("a REJECTED decision must never carry an order")
        elif self.outcome is DecisionOutcome.APPROVED:
            if self.order is None:
                raise ValueError("an APPROVED decision must carry an order")
        elif self.order is None or self.expires_at is None or self.quoted_price is None:
            raise ValueError(
                "a PENDING_APPROVAL decision must carry an order, an expiry, and the "
                "price it was quoted against"
            )
        return self

    @property
    def approved(self) -> bool:
        return self.outcome is DecisionOutcome.APPROVED


def _reject(signal_id: str, limit: Limit, detail: str, /, **values: object) -> Decision:
    """Positional-only on purpose: `values` is caller-chosen, and a value named `limit`
    or `detail` would otherwise collide with these parameters at the call site."""
    return Decision(
        outcome=DecisionOutcome.REJECTED,
        signal_id=signal_id,
        rejection=Rejection(
            limit=limit, detail=detail, values={k: str(v) for k, v in values.items()}
        ),
    )


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        fee_models: dict[str, FeeModel] | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self._fee_models = fee_models or {}

    # ------------------------------------------------------------------ public

    def evaluate(
        self,
        signal: Signal,
        market: Market,
        snapshot: PortfolioSnapshot,
        *,
        now: datetime | None = None,
    ) -> Decision:
        """The single decision point. Returns a Decision; never places anything."""
        moment = now or utc_now()
        try:
            decision = self._evaluate(signal, market, snapshot, moment)
        except RiskEngineFailure:
            raise
        except Exception as exc:
            # Fail closed. An engine that cannot decide must not default to permitting.
            self._record(
                EntryKind.HALT,
                {
                    "reason": "risk_engine_exception",
                    "signal_id": signal.signal_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise RiskEngineFailure(
                f"risk engine failed evaluating {signal.signal_id}: {type(exc).__name__}: {exc}"
            ) from exc

        self._record_decision(signal, decision)
        return decision

    def validate_approval(
        self,
        decision: Decision,
        current_quote: Quote,
        *,
        now: datetime | None = None,
    ) -> Decision:
        """Re-check a pending approval at the moment of execution.

        A stale approval is a rejected approval. Both the clock and the price are
        re-checked, because an approval granted against a price that has since moved is
        an approval for a trade the operator never saw.
        """
        moment = now or utc_now()
        if decision.outcome is not DecisionOutcome.PENDING_APPROVAL:
            raise RiskEngineFailure(
                f"validate_approval called on a {decision.outcome} decision"
            )
        # Guaranteed non-None by Decision's validator for this outcome; casting rather
        # than re-checking keeps the invariant in one place instead of two.
        order = cast(ProposedOrder, decision.order)
        expires_at = cast(datetime, decision.expires_at)
        quoted_price = cast(Probability, decision.quoted_price)

        if moment > expires_at:
            return _reject(
                decision.signal_id,
                Limit.APPROVAL_EXPIRED,
                f"approval expired at {expires_at.isoformat()}",
                now=moment.isoformat(),
            )

        touch = current_quote.touch(order.side)
        if touch is None:
            return _reject(
                decision.signal_id,
                Limit.NO_BOOK_LIQUIDITY,
                "no price on the book at execution time",
            )

        drift = abs(touch.value - quoted_price.value)
        if drift > self.config.approval_max_price_drift:
            return _reject(
                decision.signal_id,
                Limit.APPROVAL_PRICE_MOVED,
                f"price moved {drift} since approval; limit is "
                f"{self.config.approval_max_price_drift}",
                approved_at_price=quoted_price,
                current_price=touch,
            )

        approved = Decision(
            outcome=DecisionOutcome.APPROVED, signal_id=decision.signal_id, order=order
        )
        self._record(
            EntryKind.APPROVAL_GRANTED,
            {"signal_id": decision.signal_id, "idempotency_key": order.idempotency_key},
        )
        return approved

    # ----------------------------------------------------------------- private

    def _evaluate(
        self, signal: Signal, market: Market, snapshot: PortfolioSnapshot, moment: datetime
    ) -> Decision:
        config = self.config
        sid = signal.signal_id

        # --- 0. Sanity: the signal must actually describe this market ---------
        if market.market_key != signal.outcome.market_key or market.venue != signal.outcome.venue:
            raise RiskEngineFailure(
                f"signal {sid} references {signal.outcome.market_key} on "
                f"{signal.outcome.venue} but was given market {market.market_key} "
                f"on {market.venue}"
            )

        # --- 1. Kill switch. Before everything, always (§1.3) -----------------
        if kill_switch_engaged(config.kill_file):
            return _reject(sid, Limit.KILL_SWITCH, f"kill file present at {config.kill_file}")

        # --- 2. Master switch (§1.2) -----------------------------------------
        if not config.live_trading_enabled:
            return _reject(
                sid, Limit.LIVE_TRADING_DISABLED, "live_trading_enabled is false in risk config"
            )

        # --- 3. Sticky halts --------------------------------------------------
        system_halt = snapshot.system_halt()
        if system_halt is not None:
            return _reject(
                sid,
                Limit.SYSTEM_HALTED,
                f"system halted: {system_halt.detail}",
                halt_limit=system_halt.limit,
                tripped_at=system_halt.tripped_at.isoformat(),
            )
        strategy_halt = snapshot.strategy_halt(signal.strategy)
        if strategy_halt is not None:
            return _reject(
                sid,
                Limit.STRATEGY_DISABLED,
                f"strategy {signal.strategy} halted: {strategy_halt.detail}",
                halt_limit=strategy_halt.limit,
            )

        # --- 4. Promotion gate (§6) ------------------------------------------
        state = snapshot.strategy_states.get(signal.strategy)
        if state is not PromotionState.LIVE:
            return _reject(
                sid,
                Limit.STRATEGY_NOT_LIVE,
                f"strategy {signal.strategy} is {state or 'unregistered'}, not LIVE",
                promotion_state=str(state) if state else "unregistered",
            )

        # --- 5. Fee model must be verified before it can price an edge --------
        fee_model = self._fee_models.get(str(signal.outcome.venue))
        if fee_model is None:
            return _reject(
                sid,
                Limit.FEE_MODEL_UNVERIFIED,
                f"no fee model loaded for {signal.outcome.venue}",
            )
        if not fee_model.verified:
            return _reject(
                sid,
                Limit.FEE_MODEL_UNVERIFIED,
                f"fee model for {signal.outcome.venue} is unverified "
                f"(source: {fee_model.config.source}); see docs/api-notes.md",
            )

        # --- 6. Reconciliation gate — trade only on state we trust ------------
        if not snapshot.reconciliation_ok:
            return _reject(
                sid, Limit.RECONCILIATION_DIVERGED, "local state diverges from venue state"
            )
        if snapshot.last_reconciled_at is None:
            return _reject(sid, Limit.RECONCILIATION_STALE, "no reconciliation has completed")
        reconcile_age = moment - snapshot.last_reconciled_at
        if reconcile_age > timedelta(seconds=config.max_reconciliation_age_seconds):
            return _reject(
                sid,
                Limit.RECONCILIATION_STALE,
                f"last reconciliation {reconcile_age.total_seconds():.0f}s ago",
                max_age_seconds=config.max_reconciliation_age_seconds,
            )

        # --- 7. Data quality --------------------------------------------------
        if signal.quote.source is not QuoteSource.BOOK:
            return _reject(
                sid,
                Limit.QUOTE_SOURCE_NOT_BOOK,
                f"signal sized off {signal.quote.source} price; only BOOK may size a trade",
            )
        quote_age = moment - signal.quote.observed_at
        if quote_age > timedelta(seconds=config.max_quote_age_seconds):
            return _reject(
                sid,
                Limit.QUOTE_STALE,
                f"quote is {quote_age.total_seconds():.1f}s old",
                max_age_seconds=config.max_quote_age_seconds,
            )
        touch = signal.quote.touch(signal.side)
        visible = signal.quote.visible_quantity(signal.side)
        if touch is None or visible <= 0:
            return _reject(
                sid, Limit.NO_BOOK_LIQUIDITY, f"no visible {signal.side} liquidity on the book"
            )

        # --- 8. Time to close -------------------------------------------------
        seconds_to_close = (market.close_time - moment).total_seconds()
        if seconds_to_close < config.min_time_to_close_seconds:
            return _reject(
                sid,
                Limit.MIN_TIME_TO_CLOSE,
                f"{seconds_to_close:.0f}s to close is below minimum",
                min_seconds=config.min_time_to_close_seconds,
            )
        if seconds_to_close > config.max_time_to_close_seconds:
            return _reject(
                sid,
                Limit.MAX_TIME_TO_CLOSE,
                f"{seconds_to_close:.0f}s to close exceeds maximum; capital would be locked",
                max_seconds=config.max_time_to_close_seconds,
            )

        # --- 9. Order type ----------------------------------------------------
        if signal.allow_market_order and not config.allow_market_orders:
            return _reject(
                sid,
                Limit.MARKET_ORDER_NOT_PERMITTED,
                "signal requests a market order but allow_market_orders is false",
            )

        # --- 10. Slippage: is our limit price sane against the current touch? --
        slippage_bps = abs(signal.limit_price.value - touch.value) * BPS
        crosses = (
            signal.limit_price >= touch if signal.side is Side.BUY else signal.limit_price <= touch
        )
        if crosses and slippage_bps > config.max_slippage_bps:
            return _reject(
                sid,
                Limit.MAX_SLIPPAGE_BPS,
                f"limit {signal.limit_price} is {slippage_bps:.0f}bps through the touch {touch}",
                max_slippage_bps=config.max_slippage_bps,
            )

        # --- 11. Order rate limits -------------------------------------------
        if snapshot.orders_last_minute >= config.max_orders_per_minute:
            return _reject(
                sid,
                Limit.MAX_ORDERS_PER_MINUTE,
                f"{snapshot.orders_last_minute} orders in the last minute",
                limit=config.max_orders_per_minute,
            )
        if snapshot.orders_today >= config.max_orders_per_day:
            return _reject(
                sid,
                Limit.MAX_ORDERS_PER_DAY,
                f"{snapshot.orders_today} orders today",
                limit=config.max_orders_per_day,
            )

        # --- 12. Sizing -------------------------------------------------------
        sizing = size_position(
            thesis=signal.thesis_price,
            price=signal.limit_price,
            side=signal.side,
            bankroll=snapshot.equity,
            kelly_multiplier=config.kelly_fraction,
            max_position_value=config.max_position_size,
            max_position_pct_of_bankroll=config.max_position_pct_of_bankroll,
            signal_max_quantity=signal.max_quantity,
        )
        if sizing.kelly_fraction <= 0:
            return _reject(
                sid,
                Limit.NO_EDGE,
                f"Kelly fraction is {sizing.kelly_fraction}; no edge at this price",
                thesis=signal.thesis_price,
                limit_price=signal.limit_price,
            )
        if sizing.quantity <= 0:
            return _reject(
                sid,
                Limit.SIZE_ROUNDS_TO_ZERO,
                f"sizing rounds to zero contracts (bound by {sizing.binding_constraint})",
                binding_constraint=sizing.binding_constraint,
                bankroll=snapshot.equity,
            )

        quantity = sizing.quantity

        # --- 13. Book depth ---------------------------------------------------
        max_from_book = int(
            (Decimal(visible) * config.max_order_size_pct_of_book).to_integral_value()
        )
        if max_from_book <= 0:
            return _reject(
                sid,
                Limit.MAX_ORDER_SIZE_PCT_OF_BOOK,
                f"visible depth {visible} is too thin for any permitted order size",
                pct_of_book=config.max_order_size_pct_of_book,
            )
        quantity = min(quantity, max_from_book)

        # --- 14. Capital limits, evaluated on the *post-trade* position -------
        is_short = signal.side is Side.SELL
        new_notional = capital_at_risk(signal.limit_price, quantity, is_short=is_short)

        checks: list[tuple[bool, Limit, str, dict[str, object]]] = []

        post_outcome = snapshot.deployed_on_outcome(signal.outcome.outcome_key) + new_notional
        checks.append(
            (
                post_outcome > config.max_position_size,
                Limit.MAX_POSITION_SIZE,
                f"position would be {post_outcome}, cap is {config.max_position_size}",
                {"existing": snapshot.deployed_on_outcome(signal.outcome.outcome_key)},
            )
        )

        # Sizing has already rejected a non-positive bankroll (SIZE_ROUNDS_TO_ZERO), so
        # equity is strictly positive here and the ratio is always defined. Guarding it
        # again would add a branch that no input can reach, which is worse than useless:
        # it looks like a safety check while being dead code.
        position_pct = post_outcome.ratio_to(snapshot.equity)
        checks.append(
            (
                position_pct > config.max_position_pct_of_bankroll,
                Limit.MAX_POSITION_PCT_OF_BANKROLL,
                f"position would be {position_pct:.4f} of bankroll, cap is "
                f"{config.max_position_pct_of_bankroll}",
                {"equity": snapshot.equity, "position": post_outcome},
            )
        )

        post_total = snapshot.deployed_total + new_notional
        checks.append(
            (
                post_total > config.max_total_deployed,
                Limit.MAX_TOTAL_DEPLOYED,
                f"total deployed would be {post_total}, cap is {config.max_total_deployed}",
                {"existing": snapshot.deployed_total},
            )
        )

        venue_cap = config.venue_cap(signal.outcome.venue)
        post_venue = snapshot.deployed_on(signal.outcome.venue) + new_notional
        checks.append(
            (
                post_venue > venue_cap,
                Limit.MAX_VENUE_DEPLOYED,
                f"{signal.outcome.venue} deployed would be {post_venue}, cap is {venue_cap}",
                {"existing": snapshot.deployed_on(signal.outcome.venue)},
            )
        )

        post_daily = snapshot.day_new_capital + new_notional
        checks.append(
            (
                post_daily > config.max_daily_new_capital,
                Limit.MAX_DAILY_NEW_CAPITAL,
                f"new capital today would be {post_daily}, cap is {config.max_daily_new_capital}",
                {"already_today": snapshot.day_new_capital},
            )
        )

        post_cash = snapshot.cash - new_notional
        checks.append(
            (
                post_cash < config.min_cash_reserve,
                Limit.MIN_CASH_RESERVE,
                f"cash would fall to {post_cash}, reserve is {config.min_cash_reserve}",
                {"cash": snapshot.cash},
            )
        )

        post_event = snapshot.deployed_on_event(signal.outcome.event_key) + new_notional
        checks.append(
            (
                post_event > config.max_single_event_exposure,
                Limit.MAX_SINGLE_EVENT_EXPOSURE,
                f"event {signal.outcome.event_key} exposure would be {post_event}, cap is "
                f"{config.max_single_event_exposure}",
                {"existing": snapshot.deployed_on_event(signal.outcome.event_key)},
            )
        )

        # Correlated exposure. An untagged market is treated as correlated with its whole
        # venue rather than as independent — the punitive default is the point, because
        # the alternative silently under-counts the exposure that actually kills accounts.
        tags = market.correlation_tags or (f"untagged:{market.venue}",)
        for tag in tags:
            post_tag = snapshot.deployed_on_tag(tag) + new_notional
            checks.append(
                (
                    post_tag > config.max_correlated_exposure,
                    Limit.MAX_CORRELATED_EXPOSURE,
                    f"correlated exposure for tag '{tag}' would be {post_tag}, cap is "
                    f"{config.max_correlated_exposure}",
                    {"tag": tag, "existing": snapshot.deployed_on_tag(tag)},
                )
            )

        # Illiquidity. Unknown volume counts as illiquid: absence of evidence about
        # depth is not evidence of depth.
        is_illiquid = (
            market.volume_24h is None or market.volume_24h < config.illiquid_volume_threshold
        )
        if is_illiquid:
            post_illiquid = snapshot.deployed_illiquid + new_notional
            illiquid_pct = post_illiquid.ratio_to(snapshot.equity)
            checks.append(
                (
                    illiquid_pct > config.max_illiquid_pct,
                    Limit.MAX_ILLIQUID_PCT,
                    f"illiquid exposure would be {illiquid_pct:.4f} of equity, cap is "
                    f"{config.max_illiquid_pct}",
                    {
                        "volume_24h": market.volume_24h if market.volume_24h else "unknown",
                        "threshold": config.illiquid_volume_threshold,
                    },
                )
            )

        for breached, limit, detail, values in checks:
            if breached:
                return _reject(sid, limit, detail, **values)

        # --- 15. Edge after fees. The last gate, because it needs final size ---
        is_maker = not crosses
        fee = fee_model.estimate(signal.limit_price, quantity, is_maker=is_maker)
        gross_edge = Usd(signal.raw_edge() * quantity)
        net_edge = gross_edge - fee
        cost = new_notional
        net_edge_bps = net_edge.ratio_to(cost) * BPS if cost > Usd.zero() else Decimal(0)

        if net_edge_bps < config.min_edge_bps_after_fees:
            return _reject(
                sid,
                Limit.MIN_EDGE_BPS_AFTER_FEES,
                f"net edge {net_edge_bps:.0f}bps after {fee} fees is below the "
                f"{config.min_edge_bps_after_fees}bps floor",
                gross_edge=gross_edge,
                fee=fee,
                quantity=quantity,
                is_maker=is_maker,
            )

        order = ProposedOrder(
            idempotency_key=self._idempotency_key(signal, quantity),
            signal_id=sid,
            strategy=signal.strategy,
            outcome=signal.outcome,
            side=signal.side,
            limit_price=signal.limit_price,
            quantity=quantity,
            time_in_force=TimeInForce.GTC,
            max_cost=cost + fee,
            estimated_fee=fee,
            kelly_raw_quantity=sizing.kelly_raw_quantity,
            kelly_capped_quantity=quantity,
            created_at=moment,
        )

        # --- 16. Approval tier ------------------------------------------------
        if cost > config.auto_approve_threshold:
            return Decision(
                outcome=DecisionOutcome.PENDING_APPROVAL,
                signal_id=sid,
                order=order,
                expires_at=moment + timedelta(seconds=config.approval_expiry_seconds),
                quoted_price=touch,
            )

        return Decision(outcome=DecisionOutcome.APPROVED, signal_id=sid, order=order)

    @staticmethod
    def _idempotency_key(signal: Signal, quantity: int) -> str:
        """Deterministic in the trade's identity.

        Re-evaluating the same signal to the same size produces the same key, so a retry
        after an ambiguous timeout cannot become a second position. Changing any term
        changes the key, because that is a different trade and deduplicating it would be
        the more dangerous error.
        """
        material = "\x1f".join(
            [
                signal.signal_id,
                signal.strategy,
                str(signal.outcome.venue),
                signal.outcome.outcome_key,
                str(signal.side),
                str(signal.limit_price),
                str(quantity),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    # -------------------------------------------------------------- audit trail

    def _record(self, kind: EntryKind, payload: dict[str, object]) -> None:
        if self.ledger is not None:
            self.ledger.append(kind, payload)

    def _record_decision(self, signal: Signal, decision: Decision) -> None:
        if self.ledger is None:
            return
        base: dict[str, object] = {
            "signal_id": signal.signal_id,
            "strategy": signal.strategy,
            "venue": str(signal.outcome.venue),
            "outcome_key": signal.outcome.outcome_key,
            "side": str(signal.side),
            "thesis_price": str(signal.thesis_price),
            "limit_price": str(signal.limit_price),
            "rationale": signal.rationale,
        }
        if decision.outcome is DecisionOutcome.REJECTED:
            rejection = cast(Rejection, decision.rejection)
            self._record(
                EntryKind.SIGNAL_REJECTED,
                {
                    **base,
                    "limit": str(rejection.limit),
                    "detail": rejection.detail,
                    "values": rejection.values,
                },
            )
        elif decision.outcome is DecisionOutcome.PENDING_APPROVAL:
            pending = cast(ProposedOrder, decision.order)
            self._record(
                EntryKind.APPROVAL_REQUESTED,
                {
                    **base,
                    "quantity": pending.quantity,
                    "max_cost": str(pending.max_cost),
                    "expires_at": cast(datetime, decision.expires_at).isoformat(),
                },
            )
        else:
            order = cast(ProposedOrder, decision.order)
            self._record(
                EntryKind.ORDER_PROPOSED,
                {
                    **base,
                    "idempotency_key": order.idempotency_key,
                    "quantity": order.quantity,
                    "max_cost": str(order.max_cost),
                    "estimated_fee": str(order.estimated_fee),
                    "kelly_raw_quantity": order.kelly_raw_quantity,
                    "kelly_capped_quantity": order.kelly_capped_quantity,
                },
            )

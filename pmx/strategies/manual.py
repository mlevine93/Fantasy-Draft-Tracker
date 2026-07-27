"""The `manual` strategy: operator thesis in, risk-checked signal out.

This is the one that gets used on day one, and it is how the risk engine gets tested with
real money before anything autonomous runs. It contains no opinion of its own — the edge,
if any, is the operator's. What it does is force that opinion through the identical path
an automated strategy would take: same signal type, same risk engine, same limits, same
audit trail, same rejection reasons.

That equivalence is the point. If the manual path had a shortcut, the risk engine would
be least tested exactly where it is first used with live capital.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from pmx.core.clock import utc_now
from pmx.core.models import Market, OutcomeRef, PromotionState, Quote, Side, Signal
from pmx.core.money import Probability
from pmx.strategies.base import Strategy, StrategyContext

__all__ = ["ManualStrategy", "build_manual_signal"]

STRATEGY_NAME = "manual"


def build_manual_signal(
    *,
    outcome: OutcomeRef,
    side: Side,
    thesis_price: Probability,
    limit_price: Probability,
    max_quantity: int,
    rationale: str,
    quote: Quote,
) -> Signal:
    """Turn a typed thesis into a signal.

    The rationale is mandatory and is written to the audit ledger verbatim. Six months
    later, "why did we take that trade" should be answerable in the operator's own words
    rather than reconstructed from a price and a timestamp.
    """
    if not rationale.strip():
        raise ValueError("a manual signal requires a written rationale")
    if max_quantity <= 0:
        raise ValueError(f"max_quantity must be positive, got {max_quantity}")

    created_at = utc_now()
    # Deterministic in the trade's identity, so re-entering the same thesis against the
    # same quote produces the same signal id — and therefore the same idempotency key
    # downstream, which is what stops a repeated CLI invocation becoming two positions.
    material = "\x1f".join(
        [
            STRATEGY_NAME,
            str(outcome.venue),
            outcome.outcome_key,
            str(side),
            str(thesis_price),
            str(limit_price),
            str(max_quantity),
            quote.observed_at.isoformat(),
        ]
    )
    signal_id = f"manual-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"

    return Signal(
        signal_id=signal_id,
        strategy=STRATEGY_NAME,
        outcome=outcome,
        side=side,
        thesis_price=thesis_price,
        limit_price=limit_price,
        max_quantity=max_quantity,
        rationale=rationale.strip(),
        created_at=created_at,
        quote=quote,
    )


class ManualStrategy(Strategy):
    """Holds signals the operator has entered, and hands them over once.

    `on_tick` returns each queued signal exactly once. A manual thesis is a decision made
    at a moment, not a standing instruction — re-emitting it on every tick would turn one
    considered trade into a stream of them.
    """

    name = STRATEGY_NAME

    def __init__(self) -> None:
        self._queue: list[Signal] = []

    def enqueue(self, signal: Signal) -> None:
        if signal.strategy != STRATEGY_NAME:
            raise ValueError(f"expected a {STRATEGY_NAME} signal, got {signal.strategy}")
        self._queue.append(signal)

    def on_tick(
        self,
        markets: Sequence[Market],
        quotes: Sequence[Quote],
        context: StrategyContext,
    ) -> list[Signal]:
        if context.promotion_state is not PromotionState.LIVE:
            # The router would refuse these anyway; not emitting them keeps the ledger
            # free of signals that never had a chance.
            return []
        pending, self._queue = self._queue, []
        return pending

    def __len__(self) -> int:
        return len(self._queue)

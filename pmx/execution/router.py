"""THE EXECUTION ROUTER — the only code permitted to call a venue's order endpoints.

`tests/test_single_order_path.py` walks the package AST and fails the build if any other
module calls `place_order` or `cancel_order`. Together with the rule that only the risk
engine constructs a `ProposedOrder`, that gives exactly one path from a strategy's
opinion to an order at a venue, with the risk engine in the middle of it.

The sequence in `submit()` is deliberate and its order is the point:

1. Kill switch. Before every single order, no exceptions (§1.3).
2. Persist `PENDING_SUBMIT` to disk **before** the network call. If the process dies at
   any point after this, recovery finds a row it cannot explain and asks the venue.
3. Send exactly once. Never retried here or below (see venues/transport.py).
4. An ambiguous failure marks the order `UNKNOWN` and raises. `UNKNOWN` is not "failed" —
   it means the order may exist, and the only way to find out is to ask.

The thing this module refuses to do is guess. A timeout is not a rejection.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from pmx.audit.ledger import EntryKind, Ledger
from pmx.core.models import OrderState
from pmx.execution.store import OrderRecord, OrderStore
from pmx.risk.circuit import assert_kill_switch_clear
from pmx.risk.engine import Decision, DecisionOutcome
from pmx.venues.base import TradingVenue, VenueError, VenueUnavailable

__all__ = ["ExecutionResult", "ExecutionRouter", "OrderOutcomeUnknown"]


class OrderOutcomeUnknown(RuntimeError):
    """The order may or may not exist at the venue.

    Raised after an ambiguous transport failure. Trading halts: every limit downstream is
    computed from local position state, and this is precisely the condition under which
    local state is a guess. Recovery resolves it against the venue before anything else
    happens.
    """


class ExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record: OrderRecord
    venue_order_id: str | None


class ExecutionRouter:
    def __init__(
        self,
        venues: dict[str, TradingVenue],
        store: OrderStore,
        ledger: Ledger,
        *,
        kill_file: str,
    ) -> None:
        self._venues = venues
        self._store = store
        self._ledger = ledger
        self._kill_file = kill_file

    def submit(self, decision: Decision) -> ExecutionResult:
        """Place one approved order. The only way an order reaches a venue."""
        if decision.outcome is not DecisionOutcome.APPROVED or decision.order is None:
            # Belt and braces: Decision's own validator already guarantees this pairing.
            raise ValueError(
                f"router received a {decision.outcome} decision; only APPROVED may execute"
            )

        order = decision.order

        # 1. Kill switch, before everything.
        assert_kill_switch_clear(self._kill_file)

        venue = self._venues.get(str(order.outcome.venue))
        if venue is None:
            raise ValueError(f"no trading venue configured for {order.outcome.venue}")

        # 2. Intent hits disk before the wire. A DuplicateOrder here means this exact
        #    order was already attempted, and the correct response is to look it up
        #    rather than to send it again.
        self._store.record_intent(order)
        self._ledger.append(
            EntryKind.ORDER_SUBMITTED,
            {
                "idempotency_key": order.idempotency_key,
                "signal_id": order.signal_id,
                "strategy": order.strategy,
                "venue": str(order.outcome.venue),
                "outcome_key": order.outcome.outcome_key,
                "side": str(order.side),
                "limit_price": str(order.limit_price),
                "quantity": order.quantity,
                "max_cost": str(order.max_cost),
            },
        )

        # 3. Exactly one attempt.
        try:
            venue_order_id = venue.place_order(order)
        except VenueUnavailable as exc:
            # 4. Ambiguous. The order may exist. Do not retry, do not mark it failed.
            self._store.mark(order.idempotency_key, OrderState.UNKNOWN)
            self._ledger.append(
                EntryKind.ORDER_UNKNOWN,
                {
                    "idempotency_key": order.idempotency_key,
                    "error": f"{type(exc).__name__}: {exc}",
                    "resolution": "query venue state before any further action",
                },
            )
            raise OrderOutcomeUnknown(
                f"order {order.idempotency_key} is in an unknown state at "
                f"{order.outcome.venue}: {exc}"
            ) from exc
        except VenueError as exc:
            # An explicit rejection is unambiguous: the venue answered, and said no.
            updated = self._store.mark(order.idempotency_key, OrderState.REJECTED)
            self._ledger.append(
                EntryKind.ORDER_ACKED,
                {
                    "idempotency_key": order.idempotency_key,
                    "state": str(OrderState.REJECTED),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return ExecutionResult(record=updated, venue_order_id=None)

        updated = self._store.mark(
            order.idempotency_key, OrderState.SUBMITTED, venue_order_id=str(venue_order_id)
        )
        self._ledger.append(
            EntryKind.ORDER_ACKED,
            {
                "idempotency_key": order.idempotency_key,
                "venue_order_id": str(venue_order_id),
                "state": str(OrderState.SUBMITTED),
            },
        )
        return ExecutionResult(record=updated, venue_order_id=str(venue_order_id))

    def cancel(self, idempotency_key: str) -> OrderRecord:
        """Cancel a live order. Also router-only."""
        assert_kill_switch_clear(self._kill_file)
        record = self._store.get(idempotency_key)
        if record.is_terminal:
            return record
        venue = self._venues.get(str(record.venue))
        if venue is None:
            raise ValueError(f"no trading venue configured for {record.venue}")

        try:
            venue.cancel_order(record.venue_order_id or record.idempotency_key)
        except VenueUnavailable as exc:
            # A cancel whose outcome is unknown leaves the order possibly live. Same
            # rule as a submit: do not assume, go and look.
            self._store.mark(idempotency_key, OrderState.UNKNOWN)
            raise OrderOutcomeUnknown(
                f"cancel of {idempotency_key} returned an ambiguous failure: {exc}"
            ) from exc

        updated = self._store.mark(idempotency_key, OrderState.CANCELED)
        self._ledger.append(
            EntryKind.ORDER_CANCELED, {"idempotency_key": idempotency_key}
        )
        return updated

    def cancel_all(self) -> list[OrderRecord]:
        """Used by the kill path. Cancels every non-terminal order it can.

        The kill switch check is skipped *inside the loop* on purpose: this is the
        function that runs when the kill switch is already engaged, and refusing to
        cancel because we are halting would leave live orders on the book — the exact
        situation the kill switch exists to prevent.
        """
        cancelled: list[OrderRecord] = []
        for record in self._store.unresolved():
            venue = self._venues.get(str(record.venue))
            if venue is None:
                continue
            try:
                venue.cancel_order(record.venue_order_id or record.idempotency_key)
            except VenueError as exc:
                self._ledger.append(
                    EntryKind.ORDER_UNKNOWN,
                    {
                        "idempotency_key": record.idempotency_key,
                        "error": f"cancel_all could not cancel: {exc}",
                    },
                )
                continue
            cancelled.append(self._store.mark(record.idempotency_key, OrderState.CANCELED))
        return cancelled

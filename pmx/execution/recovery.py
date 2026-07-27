"""Crash recovery.

§8 asks for a test that `kill -9`s the process mid-order-placement, restarts, and proves
the system works out whether that order exists before doing anything else. This is the
code that test exercises.

The rule: **on startup, no order may be placed until every non-terminal order in the
store has been resolved against the venue.** Not "probably fine because the response
never came" — resolved, by asking. A crash between writing intent and reading the
response is indistinguishable from a crash after a successful placement, so the local
record is not evidence either way.

An order that cannot be resolved halts the system. That is the correct outcome: it means
we do not know our own position, and every risk limit is computed from position state.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from pmx.audit.ledger import EntryKind, Ledger
from pmx.core.clock import utc_now
from pmx.core.models import OrderState
from pmx.execution.store import OrderRecord, OrderStore
from pmx.risk.circuit import Halt, HaltScope
from pmx.risk.limits import Limit
from pmx.venues.base import TradingVenue, VenueError

__all__ = ["RecoveryReport", "recover_orders"]

#: Venue status strings that establish a terminal state. Anything not listed here leaves
#: the order unresolved, which halts — an unrecognised status is not evidence of anything.
_TERMINAL_STATUS: Final = {
    "filled": OrderState.FILLED,
    "executed": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.REJECTED,
}


class RecoveryReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inspected: int
    resolved_live: tuple[str, ...] = ()
    resolved_absent: tuple[str, ...] = ()
    unresolvable: tuple[str, ...] = ()
    halts: tuple[Halt, ...] = ()

    @property
    def safe_to_trade(self) -> bool:
        return not self.unresolvable and not self.halts


def recover_orders(
    store: OrderStore, venues: dict[str, TradingVenue], ledger: Ledger
) -> RecoveryReport:
    """Resolve every non-terminal order against its venue. Call before anything else."""
    pending = store.unresolved()
    live: list[str] = []
    absent: list[str] = []
    unresolvable: list[str] = []
    halts: list[Halt] = []

    for record in pending:
        venue = venues.get(str(record.venue))
        if venue is None:
            unresolvable.append(record.idempotency_key)
            halts.append(_halt(record, "no venue client configured to resolve this order"))
            continue

        try:
            open_orders = venue.open_orders()
        except VenueError as exc:
            # Cannot ask, so cannot know. Halting is the only honest response; assuming
            # the order does not exist is how a forgotten position gets doubled.
            unresolvable.append(record.idempotency_key)
            halts.append(_halt(record, f"venue unreachable during recovery: {exc}"))
            continue

        match = _find(open_orders, record)
        if match is not None:
            store.mark(
                record.idempotency_key,
                OrderState.SUBMITTED,
                venue_order_id=str(match.get("venue_order_id") or match.get("order_id") or ""),
            )
            live.append(record.idempotency_key)
            ledger.append(
                EntryKind.RECONCILIATION,
                {
                    "phase": "recovery",
                    "idempotency_key": record.idempotency_key,
                    "finding": "order exists at venue",
                },
            )
            continue

        # Not on the book. It was either never accepted, or it already filled or was
        # cancelled — and those are very different. Only a venue that can answer for a
        # closed order lets us tell them apart.
        resolved = _resolve_closed(venue, record)
        if resolved is None:
            unresolvable.append(record.idempotency_key)
            halts.append(
                _halt(
                    record,
                    "order is not on the venue's book and its terminal state could not "
                    "be established; position state is unknown",
                )
            )
            continue

        store.mark(record.idempotency_key, resolved)
        absent.append(record.idempotency_key)
        ledger.append(
            EntryKind.RECONCILIATION,
            {
                "phase": "recovery",
                "idempotency_key": record.idempotency_key,
                "finding": f"resolved to {resolved}",
            },
        )

    return RecoveryReport(
        inspected=len(pending),
        resolved_live=tuple(live),
        resolved_absent=tuple(absent),
        unresolvable=tuple(unresolvable),
        halts=tuple(halts),
    )


def _find(open_orders: list[object], record: OrderRecord) -> dict[str, object] | None:
    """Match by idempotency key, never by price and size.

    Matching on attributes would happily pair our order with somebody else's identical
    one, or with a second copy of our own. The idempotency key is the only identifier
    that means "this exact intent".
    """
    for entry in open_orders:
        if not isinstance(entry, dict):
            continue
        if entry.get("idempotency_key") == record.idempotency_key:
            return entry
    return None


def _resolve_closed(venue: TradingVenue, record: OrderRecord) -> OrderState | None:
    """Ask the venue what became of an order that is no longer open."""
    lookup = getattr(venue, "get_order", None)
    if lookup is None:
        return None
    try:
        found = lookup(record.idempotency_key)
    except VenueError:
        return None
    if found is None:
        # The venue has no record at all: the request never landed.
        return OrderState.REJECTED
    status = str(found.get("status", "")).lower() if isinstance(found, dict) else ""
    return _TERMINAL_STATUS.get(status)


def _halt(record: OrderRecord, detail: str) -> Halt:
    return Halt(
        scope=HaltScope.SYSTEM,
        limit=Limit.RECONCILIATION_DIVERGED,
        detail=f"order {record.idempotency_key}: {detail}",
        tripped_at=utc_now(),
    )

"""A local venue that behaves badly on demand.

Lives in tests/ rather than pmx/ so no fake can ever be constructed by production code.
It implements `TradingVenue` so the router is exercised against the real interface, and
it can be told to fail in the specific ways that actually lose money:

* accept an order and *then* time out, so the caller cannot tell whether it landed
* reject outright
* go unreachable during recovery or reconciliation
* report positions that disagree with ours
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pmx.core.models import Market, OutcomeRef, ProposedOrder, Quote, Venue
from pmx.core.money import Usd
from pmx.venues.base import TradingVenue, VenueError, VenueUnavailable


class FakeVenue(TradingVenue):
    """In-memory venue with injectable failures."""

    def __init__(self, venue: Venue = Venue.KALSHI) -> None:
        self.venue = venue
        self.orders: dict[str, dict[str, Any]] = {}
        self.place_calls: list[ProposedOrder] = []
        self.cancel_calls: list[str] = []
        self._positions: list[dict[str, Any]] = []
        self._balance = Usd("1000")

        # -- failure injection --
        #: Accept the order into the book, then raise as if the response was lost.
        self.timeout_after_accepting = False
        #: Raise before accepting anything.
        self.timeout_before_accepting = False
        #: Reject explicitly, the unambiguous case.
        self.reject_with: str | None = None
        #: Fail reads used by recovery and reconciliation.
        self.reads_fail = False
        #: Hide closed orders, so recovery cannot establish a terminal state.
        self.forget_closed_orders = False

    # -- trading ------------------------------------------------------------

    def place_order(self, order: ProposedOrder) -> str:
        self.place_calls.append(order)

        if self.timeout_before_accepting:
            raise VenueUnavailable("connection reset before the order was accepted")

        if self.reject_with is not None:
            self.orders[order.idempotency_key] = {
                "idempotency_key": order.idempotency_key,
                "status": "rejected",
            }
            raise VenueError(f"venue rejected the order: {self.reject_with}")

        venue_order_id = f"venue-{len(self.orders) + 1}"
        self.orders[order.idempotency_key] = {
            "idempotency_key": order.idempotency_key,
            "venue_order_id": venue_order_id,
            "status": "open",
            "quantity": order.quantity,
            "outcome_key": order.outcome.outcome_key,
        }

        if self.timeout_after_accepting:
            # The dangerous case: the order exists, and we will never learn that from
            # this call. Only a lookup against the venue can resolve it.
            raise VenueUnavailable("read timeout after the venue accepted the order")

        return venue_order_id

    def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        for entry in self.orders.values():
            if order_id in (entry.get("venue_order_id"), entry.get("idempotency_key")):
                entry["status"] = "canceled"
                return
        raise VenueError(f"no such order {order_id}")

    def open_orders(self) -> list[Any]:
        if self.reads_fail:
            raise VenueUnavailable("venue unreachable")
        return [entry for entry in self.orders.values() if entry["status"] == "open"]

    def get_order(self, idempotency_key: str) -> dict[str, Any] | None:
        if self.reads_fail or self.forget_closed_orders:
            raise VenueUnavailable("venue unreachable")
        return self.orders.get(idempotency_key)

    def positions(self) -> list[Any]:
        if self.reads_fail:
            raise VenueUnavailable("venue unreachable")
        return list(self._positions)

    def set_positions(self, positions: dict[str, int]) -> None:
        self._positions = [
            {"outcome_key": key, "quantity": quantity} for key, quantity in positions.items()
        ]

    def fill(self, idempotency_key: str, quantity: int | None = None) -> None:
        entry = self.orders[idempotency_key]
        entry["status"] = "filled"
        entry["filled_quantity"] = quantity if quantity is not None else entry["quantity"]

    def balance(self) -> Usd:
        if self.reads_fail:
            raise VenueUnavailable("venue unreachable")
        return self._balance

    # -- market data (unused by these tests, required by the interface) -----

    def server_time(self) -> datetime:
        return datetime.now(UTC)

    def list_markets(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Market], str | None]:
        return [], None

    def get_market(self, market_key: str) -> Market:
        raise NotImplementedError

    def get_quote(self, outcome: OutcomeRef) -> Quote:
        raise NotImplementedError

    def close(self) -> None:
        return None

"""Order state, persisted before the network call.

The ordering is the whole design: an order row reaches disk in `PENDING_SUBMIT` *before*
the request leaves the process. If we die between those two moments, recovery finds a
row it cannot explain and goes and asks the venue — which is the only correct answer,
because from inside the process a crash before the response is indistinguishable from a
crash after it.

The idempotency key is the primary key. A second attempt to insert the same key is a
database error, not a second order, so duplicate submission is prevented by the storage
engine rather than by remembering to check.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from pmx.core.clock import utc_now
from pmx.core.models import OrderState, ProposedOrder, Side, Venue
from pmx.core.money import Probability

__all__ = ["DuplicateOrder", "OrderRecord", "OrderStore"]

#: States from which an order may still change without us doing anything.
NON_TERMINAL: Final = frozenset(
    {
        OrderState.PENDING_SUBMIT,
        OrderState.SUBMITTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.UNKNOWN,
    }
)


class DuplicateOrder(RuntimeError):
    """This idempotency key already exists. Never place it again; go and look it up."""


class OrderRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str
    signal_id: str
    strategy: str
    venue: Venue
    market_key: str
    outcome_key: str
    side: Side
    limit_price: Probability
    quantity: int
    filled_quantity: int
    state: OrderState
    venue_order_id: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state not in NON_TERMINAL


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS orders (
    idempotency_key TEXT PRIMARY KEY,
    signal_id       TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    venue           TEXT    NOT NULL,
    market_key      TEXT    NOT NULL,
    outcome_key     TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    limit_price     TEXT    NOT NULL,
    quantity        INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    state           TEXT    NOT NULL,
    venue_order_id  TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_state_idx ON orders(state);
"""


class OrderStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # An order row that is not on disk when we crash describes an order we may have
        # placed. Durability beats throughput by a margin that is not close.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> OrderStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def record_intent(self, order: ProposedOrder) -> OrderRecord:
        """Write `PENDING_SUBMIT` to disk. Must happen before the request is sent."""
        now = utc_now()
        try:
            self._conn.execute(
                "INSERT INTO orders (idempotency_key, signal_id, strategy, venue, market_key, "
                "outcome_key, side, limit_price, quantity, filled_quantity, state, "
                "venue_order_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?)",
                (
                    order.idempotency_key,
                    order.signal_id,
                    order.strategy,
                    str(order.outcome.venue),
                    order.outcome.market_key,
                    order.outcome.outcome_key,
                    str(order.side),
                    str(order.limit_price),
                    order.quantity,
                    str(OrderState.PENDING_SUBMIT),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateOrder(
                f"idempotency key {order.idempotency_key} already exists: this order was "
                "already attempted. Query its state; never resubmit blind."
            ) from exc
        return self.get(order.idempotency_key)

    def get(self, idempotency_key: str) -> OrderRecord:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no order with idempotency key {idempotency_key}")
        return self._to_record(row)

    def mark(
        self,
        idempotency_key: str,
        state: OrderState,
        *,
        venue_order_id: str | None = None,
        filled_quantity: int | None = None,
    ) -> OrderRecord:
        existing = self.get(idempotency_key)
        if existing.is_terminal and state != existing.state:
            # A terminal order does not change. Silently overwriting one would erase the
            # record of what actually happened, which is the opposite of an audit trail.
            raise ValueError(
                f"order {idempotency_key} is terminal in state {existing.state}; "
                f"refusing to move it to {state}"
            )
        self._conn.execute(
            "UPDATE orders SET state = ?, venue_order_id = COALESCE(?, venue_order_id), "
            "filled_quantity = COALESCE(?, filled_quantity), updated_at = ? "
            "WHERE idempotency_key = ?",
            (
                str(state),
                venue_order_id,
                filled_quantity,
                utc_now().isoformat(),
                idempotency_key,
            ),
        )
        return self.get(idempotency_key)

    def unresolved(self) -> list[OrderRecord]:
        """Orders whose true state we do not know. The recovery path's work list."""
        placeholders = ",".join("?" for _ in NON_TERMINAL)
        rows = self._conn.execute(
            f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY created_at ASC",
            tuple(str(state) for state in sorted(NON_TERMINAL)),
        ).fetchall()
        return [self._to_record(row) for row in rows]

    def all_orders(self) -> list[OrderRecord]:
        rows = self._conn.execute("SELECT * FROM orders ORDER BY created_at ASC").fetchall()
        return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            idempotency_key=str(row["idempotency_key"]),
            signal_id=str(row["signal_id"]),
            strategy=str(row["strategy"]),
            venue=Venue(row["venue"]),
            market_key=str(row["market_key"]),
            outcome_key=str(row["outcome_key"]),
            side=Side(row["side"]),
            limit_price=Probability(str(row["limit_price"])),
            quantity=int(row["quantity"]),
            filled_quantity=int(row["filled_quantity"]),
            state=OrderState(row["state"]),
            venue_order_id=row["venue_order_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

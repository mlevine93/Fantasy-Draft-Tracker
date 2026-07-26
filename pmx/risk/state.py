"""Risk state: the snapshot the engine reasons over, and the store that makes halts stick.

The engine is a pure function of `(config, signal, PortfolioSnapshot)`. That is a
deliberate constraint: it means every rejection is reproducible from data recorded in
the ledger, and the property-based tests can generate portfolio states directly instead
of constructing a database.

Stickiness lives here rather than in the engine, because a halt held in a live object
disappears on restart — and "restart the process" is exactly what an operator does when
a system stops trading. Halts are rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from pmx.core.clock import utc_now
from pmx.core.models import PromotionState, Venue
from pmx.core.money import Usd
from pmx.risk.circuit import Halt, HaltScope
from pmx.risk.limits import Limit

__all__ = ["HaltStore", "PortfolioSnapshot"]


class PortfolioSnapshot(BaseModel):
    """Everything the risk engine needs to know about the world right now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Cash plus mark-to-market of open positions.
    equity: Usd
    #: Uncommitted cash. `min_cash_reserve` is enforced against this.
    cash: Usd
    #: Highest equity ever observed. Denominator of the drawdown halt.
    peak_equity: Usd

    #: Notional at risk, aggregated every way a limit needs it.
    deployed_total: Usd
    deployed_by_venue: dict[Venue, Usd] = Field(default_factory=dict)
    #: Keyed by `OutcomeRef.event_key`.
    deployed_by_event: dict[str, Usd] = Field(default_factory=dict)
    #: Keyed by correlation tag. One position appears under each of its tags, so these
    #: intentionally sum to more than `deployed_total`.
    deployed_by_correlation_tag: dict[str, Usd] = Field(default_factory=dict)
    #: Keyed by `OutcomeRef.outcome_key`.
    deployed_by_outcome: dict[str, Usd] = Field(default_factory=dict)
    #: Exposure to markets below the liquidity threshold.
    deployed_illiquid: Usd = Usd.zero()

    #: New capital deployed so far this UTC day.
    day_new_capital: Usd = Usd.zero()
    #: Realized plus unrealized P&L this UTC day. Negative is a loss.
    day_pnl: Usd = Usd.zero()
    strategy_pnl: dict[str, Usd] = Field(default_factory=dict)

    orders_last_minute: int = 0
    orders_today: int = 0

    last_reconciled_at: datetime | None = None
    reconciliation_ok: bool = False

    #: Halts currently in force, from the sticky store.
    active_halts: tuple[Halt, ...] = ()
    #: §6 promotion gate. A strategy absent from this map is not LIVE.
    strategy_states: dict[str, PromotionState] = Field(default_factory=dict)

    def system_halt(self) -> Halt | None:
        for halt in self.active_halts:
            if halt.scope is HaltScope.SYSTEM:
                return halt
        return None

    def strategy_halt(self, strategy: str) -> Halt | None:
        for halt in self.active_halts:
            if halt.scope is HaltScope.STRATEGY and halt.strategy == strategy:
                return halt
        return None

    def deployed_on(self, venue: Venue) -> Usd:
        return self.deployed_by_venue.get(venue, Usd.zero())

    def deployed_on_event(self, event_key: str) -> Usd:
        return self.deployed_by_event.get(event_key, Usd.zero())

    def deployed_on_outcome(self, outcome_key: str) -> Usd:
        return self.deployed_by_outcome.get(outcome_key, Usd.zero())

    def deployed_on_tag(self, tag: str) -> Usd:
        return self.deployed_by_correlation_tag.get(tag, Usd.zero())


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS halts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL,
    limit_name  TEXT    NOT NULL,
    detail      TEXT    NOT NULL,
    strategy    TEXT,
    tripped_at  TEXT    NOT NULL,
    cleared_at  TEXT,
    cleared_by  TEXT,
    clear_note  TEXT
);
CREATE INDEX IF NOT EXISTS halts_active_idx ON halts(cleared_at);
"""


class HaltStore:
    """Persistent, sticky halts.

    There is no `clear_all()` and no expiry. Clearing is one halt at a time, by id, with
    an operator name and a note that lands in the ledger — because the useful question
    after an incident is not "was it cleared" but "who decided it was safe, and why".
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> HaltStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def trip(self, halt: Halt) -> int:
        """Record a halt. Returns its id. Duplicate halts are not deduplicated — the
        second occurrence of the same breaker is information, not noise."""
        cursor = self._conn.execute(
            "INSERT INTO halts (scope, limit_name, detail, strategy, tripped_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(halt.scope),
                str(halt.limit),
                halt.detail,
                halt.strategy,
                halt.tripped_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def active(self) -> tuple[Halt, ...]:
        rows = self._conn.execute(
            "SELECT * FROM halts WHERE cleared_at IS NULL ORDER BY id ASC"
        ).fetchall()
        return tuple(
            Halt(
                scope=HaltScope(row["scope"]),
                limit=Limit(row["limit_name"]),
                detail=str(row["detail"]),
                strategy=row["strategy"],
                tripped_at=datetime.fromisoformat(row["tripped_at"]),
            )
            for row in rows
        )

    def clear(self, halt_id: int, *, operator: str, note: str) -> bool:
        """Manually clear one halt. Returns False if it was not active."""
        if not operator.strip():
            raise ValueError("clearing a halt requires an operator name")
        if not note.strip():
            raise ValueError("clearing a halt requires a note explaining why it is safe")
        cursor = self._conn.execute(
            "UPDATE halts SET cleared_at = ?, cleared_by = ?, clear_note = ? "
            "WHERE id = ? AND cleared_at IS NULL",
            (utc_now().isoformat(), operator, note, halt_id),
        )
        return cursor.rowcount > 0

    def history(self, limit: int = 100) -> list[Mapping[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM halts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

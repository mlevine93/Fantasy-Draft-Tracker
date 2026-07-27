"""Promotion gate (§6).

A strategy's `promotion_state` is the thing standing between "it looked good in a
backtest" and "it is spending money". The risk engine already rejects any signal from a
strategy that is not `LIVE`; this module is where that state lives and how it changes.

Two rules:

* **Promotion is manual.** There is no code path that advances a strategy to `LIVE`
  automatically, and no threshold that does it on the strategy's behalf. The operator
  types the command, names themselves, and writes why.
* **Demotion is not.** Anything may demote a strategy — a loss halt, a failed
  reconciliation, an operator. Making the safe direction easy and the dangerous
  direction deliberate is the whole point of the asymmetry.

Every transition lands in the audit ledger with who and why, because "when did this
start trading, and on whose say-so" is a question that gets asked after a bad week.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from pmx.audit.ledger import EntryKind, Ledger
from pmx.core.clock import utc_now
from pmx.core.models import PromotionState

__all__ = ["PromotionError", "PromotionRecord", "PromotionStore"]

#: The only transition that may put a strategy in front of real money, and the states it
#: is allowed to come from. A strategy cannot jump from BACKTEST straight to LIVE: §6
#: requires paper trading against live data in between, and skipping it is exactly the
#: shortcut a deadline tempts you into.
_ALLOWED_TO_LIVE: Final = frozenset({PromotionState.PAPER, PromotionState.SHADOW})


class PromotionError(RuntimeError):
    """An invalid promotion was attempted."""


class PromotionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: str
    state: PromotionState
    operator: str
    note: str
    changed_at: datetime


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS promotions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy   TEXT NOT NULL,
    state      TEXT NOT NULL,
    operator   TEXT NOT NULL,
    note       TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS promotions_strategy_idx ON promotions(strategy, id);
"""


class PromotionStore:
    """Append-only promotion history; current state is the latest row per strategy."""

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

    def __enter__(self) -> PromotionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def state_of(self, strategy: str) -> PromotionState:
        """Current state. An unregistered strategy is `BACKTEST`, never `LIVE`."""
        row = self._conn.execute(
            "SELECT state FROM promotions WHERE strategy = ? ORDER BY id DESC LIMIT 1",
            (strategy,),
        ).fetchone()
        return PromotionState(row["state"]) if row else PromotionState.BACKTEST

    def all_states(self) -> dict[str, PromotionState]:
        """What the risk engine's snapshot needs."""
        rows = self._conn.execute(
            "SELECT strategy, state FROM promotions p WHERE id = "
            "(SELECT MAX(id) FROM promotions WHERE strategy = p.strategy)"
        ).fetchall()
        return {str(row["strategy"]): PromotionState(row["state"]) for row in rows}

    def set_state(
        self,
        strategy: str,
        state: PromotionState,
        *,
        operator: str,
        note: str,
        ledger: Ledger | None = None,
    ) -> PromotionRecord:
        if not strategy.strip():
            raise PromotionError("strategy name is required")
        if not operator.strip():
            raise PromotionError("promoting or demoting a strategy requires an operator name")
        if not note.strip():
            raise PromotionError("a promotion requires a written justification")

        current = self.state_of(strategy)
        if state is PromotionState.LIVE and current not in _ALLOWED_TO_LIVE:
            raise PromotionError(
                f"{strategy} is {current}; a strategy may only go LIVE from "
                f"{sorted(str(allowed) for allowed in _ALLOWED_TO_LIVE)}. §6 requires "
                "paper trading against live data before real capital."
            )

        changed_at = utc_now()
        self._conn.execute(
            "INSERT INTO promotions (strategy, state, operator, note, changed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (strategy, str(state), operator, note, changed_at.isoformat()),
        )
        if ledger is not None:
            ledger.append(
                EntryKind.CONFIG_CHANGED,
                {
                    "change": "promotion_state",
                    "strategy": strategy,
                    "from": str(current),
                    "to": str(state),
                    "operator": operator,
                    "note": note,
                },
            )
        return PromotionRecord(
            strategy=strategy,
            state=state,
            operator=operator,
            note=note,
            changed_at=changed_at,
        )

    def disable(
        self, strategy: str, *, reason: str, ledger: Ledger | None = None
    ) -> PromotionRecord:
        """Demote to DISABLED. Deliberately easy: any breaker may call this.

        No operator name is required because a machine may do it — and a system that
        needed a human to disable a losing strategy would keep trading while it waited.
        """
        return self.set_state(
            strategy,
            PromotionState.DISABLED,
            operator="system",
            note=reason,
            ledger=ledger,
        )

    def history(self, strategy: str | None = None, limit: int = 50) -> list[PromotionRecord]:
        if strategy:
            rows = self._conn.execute(
                "SELECT * FROM promotions WHERE strategy = ? ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM promotions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            PromotionRecord(
                strategy=str(row["strategy"]),
                state=PromotionState(row["state"]),
                operator=str(row["operator"]),
                note=str(row["note"]),
                changed_at=datetime.fromisoformat(row["changed_at"]),
            )
            for row in rows
        ]

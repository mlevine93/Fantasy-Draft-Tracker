"""Reconciliation: exchange truth versus local truth.

Every capital and exposure limit is computed from local position state. If local state
and the venue disagree, the limits are not wrong by the size of the discrepancy — they
are simply not limits any more, because the number they are comparing against is fiction.
So divergence beyond tolerance halts, and the risk engine independently refuses to
approve anything while reconciliation is stale or failed.

The venue is authoritative. Where they disagree, we are wrong.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from pmx.audit.ledger import EntryKind, Ledger
from pmx.core.clock import utc_now
from pmx.core.money import Usd
from pmx.risk.circuit import Halt, HaltScope
from pmx.risk.limits import Limit
from pmx.venues.base import TradingVenue, VenueError

__all__ = ["Divergence", "ReconciliationResult", "reconcile"]


class Divergence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: str
    outcome_key: str
    local_quantity: int
    venue_quantity: int

    @property
    def delta(self) -> int:
        return self.venue_quantity - self.local_quantity

    def one_line(self) -> str:
        return (
            f"{self.venue}:{self.outcome_key} local={self.local_quantity} "
            f"venue={self.venue_quantity} delta={self.delta:+d}"
        )


class ReconciliationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked: int
    divergences: tuple[Divergence, ...] = ()
    unreachable: tuple[str, ...] = ()
    halts: tuple[Halt, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.divergences and not self.unreachable


def reconcile(
    local_positions: Mapping[str, Mapping[str, int]],
    venues: Mapping[str, TradingVenue],
    ledger: Ledger,
    *,
    tolerance_contracts: int = 0,
) -> ReconciliationResult:
    """Compare local positions against each venue's own view.

    `local_positions` is `{venue: {outcome_key: quantity}}`.

    Tolerance is in **contracts, and defaults to zero**. A prediction-market position is
    a whole number of contracts; there is no rounding to absorb, so any difference is a
    real disagreement about what we own. A dollar tolerance would let a genuine missing
    fill hide inside it at low prices — a hundred contracts at three cents is three
    dollars of "tolerance" and a hundred contracts of exposure.
    """
    divergences: list[Divergence] = []
    unreachable: list[str] = []
    halts: list[Halt] = []
    checked = 0

    for venue_name, venue in sorted(venues.items()):
        try:
            reported = venue.positions()
        except VenueError as exc:
            unreachable.append(venue_name)
            halts.append(
                Halt(
                    scope=HaltScope.SYSTEM,
                    limit=Limit.RECONCILIATION_STALE,
                    detail=f"could not read positions from {venue_name}: {exc}",
                    tripped_at=utc_now(),
                )
            )
            continue

        venue_view: dict[str, int] = {}
        for entry in reported:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("outcome_key", ""))
            if key:
                venue_view[key] = int(entry.get("quantity", 0))

        local_view = dict(local_positions.get(venue_name, {}))

        # Union of both sides: a position the venue has and we do not is the dangerous
        # direction, and iterating only over local keys would never see it.
        for outcome_key in sorted(set(local_view) | set(venue_view)):
            checked += 1
            local_quantity = local_view.get(outcome_key, 0)
            venue_quantity = venue_view.get(outcome_key, 0)
            if abs(venue_quantity - local_quantity) > tolerance_contracts:
                divergences.append(
                    Divergence(
                        venue=venue_name,
                        outcome_key=outcome_key,
                        local_quantity=local_quantity,
                        venue_quantity=venue_quantity,
                    )
                )

    if divergences:
        halts.append(
            Halt(
                scope=HaltScope.SYSTEM,
                limit=Limit.RECONCILIATION_DIVERGED,
                detail="; ".join(divergence.one_line() for divergence in divergences[:5]),
                tripped_at=utc_now(),
            )
        )

    result = ReconciliationResult(
        checked=checked,
        divergences=tuple(divergences),
        unreachable=tuple(unreachable),
        halts=tuple(halts),
    )

    ledger.append(
        EntryKind.RECONCILIATION,
        {
            "phase": "periodic",
            "checked": checked,
            "ok": result.ok,
            "divergences": [divergence.one_line() for divergence in divergences],
            "unreachable": list(unreachable),
        },
    )
    return result


def reconcile_cash(local_cash: Usd, venue_cash: Usd, *, tolerance: Usd) -> Halt | None:
    """Cash is the other half. A matching position book with the wrong cash still means
    we have mispriced something — usually fees."""
    if abs((venue_cash - local_cash).amount) > tolerance.amount:
        return Halt(
            scope=HaltScope.SYSTEM,
            limit=Limit.RECONCILIATION_DIVERGED,
            detail=f"cash: local {local_cash} vs venue {venue_cash}",
            tripped_at=utc_now(),
        )
    return None

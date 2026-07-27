"""Daily digest.

Built from the audit ledger rather than from live state, on purpose: the ledger is what
actually happened, and a report that reads current memory can agree with a bug instead of
exposing it. If the digest and the running system ever disagree, the digest is right.

§8 asks for equity, day P&L, open positions, exposure by event and strategy, orders
placed and rejected with top reasons, reconciliation status, and any limit that came
within 20% of binding. The near-miss section is the one that earns its place: a limit at
95% is not yet a rejection, and it is the only warning you get before it becomes one.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from pmx.audit.ledger import EntryKind, Ledger, LedgerEntry
from pmx.core.money import Usd

__all__ = ["DailyDigest", "build_digest", "render_digest"]

#: A limit this close to binding is reported as a near miss.
NEAR_MISS_FRACTION = Decimal("0.8")


class NearMiss(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: str
    used: Usd
    cap: Usd

    @property
    def fraction(self) -> Decimal:
        return self.used.ratio_to(self.cap) if self.cap > Usd.zero() else Decimal(0)


class DailyDigest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    equity: Usd
    day_pnl: Usd
    orders_proposed: int
    orders_submitted: int
    orders_rejected: int
    fills: int
    rejection_reasons: dict[str, int]
    exposure_by_event: dict[str, Usd]
    exposure_by_strategy: dict[str, Usd]
    reconciliation_ok: bool
    reconciliation_detail: str
    halts: tuple[str, ...]
    near_misses: tuple[NearMiss, ...]
    ledger_entries: int
    ledger_verified: bool


def _entries_for_day(ledger: Ledger, day: date) -> list[LedgerEntry]:
    start = datetime.combine(day, time.min).isoformat()
    end = datetime.combine(day + timedelta(days=1), time.min).isoformat()
    return [entry for entry in ledger.iter_entries() if start <= entry.timestamp < end]


def build_digest(
    ledger: Ledger,
    day: date,
    *,
    equity: Usd,
    day_pnl: Usd,
    exposure_by_event: dict[str, Usd] | None = None,
    exposure_by_strategy: dict[str, Usd] | None = None,
    near_misses: tuple[NearMiss, ...] = (),
) -> DailyDigest:
    entries = _entries_for_day(ledger, day)

    rejections = Counter(
        str(entry.payload.get("limit", "unknown"))
        for entry in entries
        if entry.kind == str(EntryKind.SIGNAL_REJECTED)
    )

    reconciliations = [
        entry for entry in entries if entry.kind == str(EntryKind.RECONCILIATION)
    ]
    if reconciliations:
        last = reconciliations[-1]
        reconciliation_ok = bool(last.payload.get("ok", False))
        detail = str(last.payload.get("divergences") or "clean")
    else:
        # No reconciliation today is not "fine": the risk engine treats stale
        # reconciliation as a hard reject, and the digest should say so plainly.
        reconciliation_ok = False
        detail = "no reconciliation ran today"

    halts = tuple(
        str(entry.payload.get("reason") or entry.payload.get("detail") or "halt")
        for entry in entries
        if entry.kind == str(EntryKind.HALT)
    )

    try:
        ledger.verify()
        verified = True
    except Exception:
        # Reported rather than raised: a tampered ledger must appear in the digest that
        # goes to the accountant, not take down the reporting job that would reveal it.
        verified = False

    return DailyDigest(
        day=day,
        equity=equity,
        day_pnl=day_pnl,
        orders_proposed=sum(1 for e in entries if e.kind == str(EntryKind.ORDER_PROPOSED)),
        orders_submitted=sum(1 for e in entries if e.kind == str(EntryKind.ORDER_SUBMITTED)),
        orders_rejected=sum(rejections.values()),
        fills=sum(1 for e in entries if e.kind == str(EntryKind.FILL)),
        rejection_reasons=dict(rejections.most_common()),
        exposure_by_event=exposure_by_event or {},
        exposure_by_strategy=exposure_by_strategy or {},
        reconciliation_ok=reconciliation_ok,
        reconciliation_detail=detail,
        halts=halts,
        near_misses=near_misses,
        ledger_entries=len(entries),
        ledger_verified=verified,
    )


def render_digest(digest: DailyDigest) -> str:
    """Plain text. CLI only in v1 (§12): pretty comes after profitable."""
    lines = [
        f"PMX daily digest — {digest.day.isoformat()}",
        "=" * 46,
        f"equity            {digest.equity}",
        f"day P&L           {digest.day_pnl}",
        "",
        f"orders proposed   {digest.orders_proposed}",
        f"orders submitted  {digest.orders_submitted}",
        f"orders rejected   {digest.orders_rejected}",
        f"fills             {digest.fills}",
    ]

    if digest.rejection_reasons:
        lines.append("")
        lines.append("top rejection reasons")
        for reason, count in list(digest.rejection_reasons.items())[:5]:
            lines.append(f"  {count:>4}  {reason}")

    if digest.exposure_by_event:
        lines.append("")
        lines.append("exposure by event")
        for event, amount in sorted(digest.exposure_by_event.items()):
            lines.append(f"  {amount!s:>12}  {event}")

    if digest.exposure_by_strategy:
        lines.append("")
        lines.append("exposure by strategy")
        for strategy, amount in sorted(digest.exposure_by_strategy.items()):
            lines.append(f"  {amount!s:>12}  {strategy}")

    lines.append("")
    status = "OK" if digest.reconciliation_ok else "FAILED"
    lines.append(f"reconciliation    {status} — {digest.reconciliation_detail}")

    if digest.halts:
        lines.append("")
        lines.append("HALTS")
        for halt in digest.halts:
            lines.append(f"  {halt}")

    if digest.near_misses:
        lines.append("")
        lines.append("limits within 20% of binding")
        for miss in digest.near_misses:
            lines.append(f"  {miss.fraction:.0%}  {miss.limit}  ({miss.used} of {miss.cap})")

    lines.append("")
    integrity = "verified" if digest.ledger_verified else "*** INTEGRITY FAILURE ***"
    lines.append(f"ledger            {digest.ledger_entries} entries today, chain {integrity}")
    return "\n".join(lines)


def near_misses_from(limits: dict[str, tuple[Usd, Usd]]) -> tuple[NearMiss, ...]:
    """`{limit_name: (used, cap)}` filtered to those close to binding."""
    found = []
    for name, (used, cap) in sorted(limits.items()):
        if cap > Usd.zero() and used.ratio_to(cap) >= NEAR_MISS_FRACTION:
            found.append(NearMiss(limit=name, used=used, cap=cap))
    return tuple(found)

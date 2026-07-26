"""Kill switch and circuit breakers.

The kill switch is a file. Not a config key, not an environment variable, not an API —
a file whose presence stops everything, checked before every order and every loop
iteration. Nothing can disable it, because there is nothing to disable: the check is
`Path.exists()` and the only way to keep trading is to delete the file.

Halts are sticky by construction. Tripping one writes a row; clearing one requires an
explicit operator command. Nothing here auto-resumes, and there is no timeout after
which a halt lapses — a system that recovers on its own from a drawdown halt is a
system that will lose the next drawdown too.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from pmx.core.clock import utc_now
from pmx.core.money import Usd
from pmx.risk.limits import Limit, RiskConfig

__all__ = ["Halt", "HaltScope", "KillSwitchEngaged", "evaluate_breakers", "kill_switch_engaged"]


class KillSwitchEngaged(RuntimeError):
    """The KILL file exists. Cancel everything, halt, exit non-zero."""


class HaltScope(StrEnum):
    #: Stops all trading on every venue and strategy.
    SYSTEM = "system"
    #: Disables one strategy; the rest of the system continues.
    STRATEGY = "strategy"


class Halt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: HaltScope
    limit: Limit
    detail: str
    tripped_at: datetime
    strategy: str | None = None
    #: Sticky halts require the CLI to clear. All of ours are sticky; the field exists
    #: so that a future non-sticky breaker cannot be added without saying so explicitly.
    sticky: bool = True


def kill_switch_engaged(kill_file: Path | str) -> bool:
    """True if the kill file exists. Deliberately the simplest possible predicate."""
    return Path(kill_file).exists()


def assert_kill_switch_clear(kill_file: Path | str) -> None:
    if kill_switch_engaged(kill_file):
        raise KillSwitchEngaged(f"kill file present at {kill_file}: halting")


def evaluate_breakers(
    config: RiskConfig,
    *,
    equity: Usd,
    peak_equity: Usd,
    day_pnl: Usd,
    strategy_pnl: dict[str, Usd],
    last_reconciled_at: datetime | None,
    reconciliation_ok: bool,
    now: datetime | None = None,
) -> list[Halt]:
    """Return every breaker that should be tripped given current state.

    Returns halts rather than raising so the caller can record all of them — knowing
    that both the daily loss limit and the drawdown limit fired is more useful after
    the fact than knowing whichever one happened to be checked first.
    """
    moment = now or utc_now()
    halts: list[Halt] = []

    if day_pnl < -config.daily_loss_halt:
        halts.append(
            Halt(
                scope=HaltScope.SYSTEM,
                limit=Limit.DAILY_LOSS_HALT,
                detail=f"day P&L {day_pnl} breached daily_loss_halt {config.daily_loss_halt}",
                tripped_at=moment,
            )
        )

    if peak_equity > Usd.zero():
        drawdown = (peak_equity - equity).ratio_to(peak_equity)
        if drawdown > config.max_drawdown_halt:
            halts.append(
                Halt(
                    scope=HaltScope.SYSTEM,
                    limit=Limit.MAX_DRAWDOWN_HALT,
                    detail=(
                        f"drawdown {drawdown:.4f} from peak {peak_equity} to {equity} "
                        f"breached max_drawdown_halt {config.max_drawdown_halt}"
                    ),
                    tripped_at=moment,
                )
            )

    for strategy, pnl in sorted(strategy_pnl.items()):
        if pnl < -config.per_strategy_loss_halt:
            halts.append(
                Halt(
                    scope=HaltScope.STRATEGY,
                    limit=Limit.PER_STRATEGY_LOSS_HALT,
                    detail=(
                        f"strategy {strategy} P&L {pnl} breached "
                        f"per_strategy_loss_halt {config.per_strategy_loss_halt}"
                    ),
                    tripped_at=moment,
                    strategy=strategy,
                )
            )

    if not reconciliation_ok:
        halts.append(
            Halt(
                scope=HaltScope.SYSTEM,
                limit=Limit.RECONCILIATION_DIVERGED,
                detail="local position state diverges from venue-reported state",
                tripped_at=moment,
            )
        )
    elif last_reconciled_at is None:
        halts.append(
            Halt(
                scope=HaltScope.SYSTEM,
                limit=Limit.RECONCILIATION_STALE,
                detail="no reconciliation has ever completed",
                tripped_at=moment,
            )
        )
    else:
        age = moment - last_reconciled_at
        if age > timedelta(seconds=config.max_reconciliation_age_seconds):
            halts.append(
                Halt(
                    scope=HaltScope.SYSTEM,
                    limit=Limit.RECONCILIATION_STALE,
                    detail=(
                        f"last reconciliation {age.total_seconds():.0f}s ago exceeds "
                        f"max_reconciliation_age_seconds {config.max_reconciliation_age_seconds}"
                    ),
                    tripped_at=moment,
                )
            )

    return halts


def drawdown_fraction(equity: Usd, peak_equity: Usd) -> Decimal:
    """Peak-to-trough decline as a fraction. Zero peak means zero drawdown, not undefined."""
    if peak_equity <= Usd.zero():
        return Decimal(0)
    if equity >= peak_equity:
        return Decimal(0)
    return (peak_equity - equity).ratio_to(peak_equity)

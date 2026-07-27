"""Operator CLI.

Phase 0 scope: inspect config, verify the ledger, and manage halts. There is no command
that places an order, because no code that places an order exists yet.

`clear-halt` is the only command that changes trading state, and it deliberately makes
the operator type a name and a reason — both land in the audit ledger. Clearing a halt
is the moment where a human overrides a machine that decided something was wrong, which
is exactly the decision that should be on the record.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path

import typer

from pmx.audit.ledger import EntryKind, Ledger, LedgerIntegrityError
from pmx.core.clock import Clock, StaleTimestampError, utc_now
from pmx.core.ids import condition_id, token_id
from pmx.core.models import OutcomeRef
from pmx.core.money import Usd
from pmx.data.recorder import TickRecorder
from pmx.execution.store import OrderStore
from pmx.ops.heartbeat import Heartbeat
from pmx.ops.reporting import build_digest, render_digest
from pmx.risk.circuit import kill_switch_engaged
from pmx.risk.limits import ConfigError, load_risk_config
from pmx.risk.state import HaltStore
from pmx.venues.base import MarketDataVenue, VenueError
from pmx.venues.kalshi import PROD_BASE_URL, KalshiMarketData
from pmx.venues.polymarket import CLOB_BASE_URL, PolymarketMarketData

app = typer.Typer(add_completion=False, help="PMX operator CLI")

DEFAULT_CONFIG = os.environ.get("PMX_RISK_CONFIG", "risk_config.yaml")
DEFAULT_DB = os.environ.get("PMX_DB_PATH", "var/pmx.db")


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def config(path: str = DEFAULT_CONFIG) -> None:
    """Load and validate the risk config, then print what is actually in force."""
    try:
        loaded = load_risk_config(path)
    except ConfigError as exc:
        _fail(str(exc))
        return

    typer.echo(f"config:              {path}")
    typer.echo(f"live_trading_enabled {loaded.live_trading_enabled}")
    engaged = kill_switch_engaged(loaded.kill_file)
    typer.echo(f"kill_file            {loaded.kill_file} (present: {engaged})")
    typer.echo(f"max_total_deployed   {loaded.max_total_deployed}")
    typer.echo(f"max_position_size    {loaded.max_position_size}")
    typer.echo(f"daily_loss_halt      {loaded.daily_loss_halt}")
    typer.echo(f"max_drawdown_halt    {loaded.max_drawdown_halt}")
    typer.echo(f"kelly_fraction       {loaded.kelly_fraction}")
    typer.echo(f"auto_approve_thresh  {loaded.auto_approve_threshold}")
    typer.echo("")
    for venue, model in sorted(loaded.fee_models.items()):
        status = "VERIFIED" if model.verified else "UNVERIFIED — venue is hard-blocked"
        typer.echo(f"fee model {venue:<12} {status}")
        typer.echo(f"                     source: {model.source}")

    unverified = [v for v, m in loaded.fee_models.items() if not m.verified]
    if unverified:
        typer.secho(
            f"\nNo signal can be approved on {', '.join(str(v) for v in unverified)} "
            "while the fee model is unverified. See docs/api-notes.md §0.",
            fg=typer.colors.YELLOW,
        )


@app.command("verify-ledger")
def verify_ledger(db: str = DEFAULT_DB) -> None:
    """Walk the hash chain end to end and report the first break, if any."""
    if not Path(db).exists():
        _fail(f"no ledger at {db}")
    with Ledger(db) as ledger:
        try:
            count = ledger.verify()
        except LedgerIntegrityError as exc:
            _fail(f"LEDGER INTEGRITY FAILURE: {exc}")
            return
        head_seq, head_hash = ledger.head()
    typer.secho(f"ledger OK — {count} entries verified", fg=typer.colors.GREEN)
    typer.echo(f"head: seq={head_seq} hash={head_hash}")


@app.command()
def halts(db: str = DEFAULT_DB, history: bool = False) -> None:
    """List active halts. Nothing here resumes trading; use clear-halt for that."""
    with HaltStore(db) as store:
        if history:
            for row in store.history():
                state = "CLEARED" if row["cleared_at"] else "ACTIVE"
                typer.echo(
                    f"[{row['id']}] {state} {row['limit_name']} ({row['scope']}) "
                    f"tripped {row['tripped_at']} — {row['detail']}"
                )
            return
        active = store.active()
        if not active:
            typer.secho("no active halts", fg=typer.colors.GREEN)
            return
        for halt in active:
            scope = halt.strategy or halt.scope
            typer.secho(
                f"{halt.limit} [{scope}] {halt.tripped_at}: {halt.detail}", fg=typer.colors.RED
            )


@app.command("clear-halt")
def clear_halt(
    halt_id: int,
    operator: str = typer.Option(..., help="Who is clearing this"),
    note: str = typer.Option(..., help="Why it is safe to resume"),
    db: str = DEFAULT_DB,
) -> None:
    """Manually clear one halt. Halts never clear themselves (§4)."""
    with HaltStore(db) as store, Ledger(db.replace(".db", "-audit.db")) as ledger:
        try:
            cleared = store.clear(halt_id, operator=operator, note=note)
        except ValueError as exc:
            _fail(str(exc))
            return
        if not cleared:
            _fail(f"halt {halt_id} is not active")
            return
        ledger.append(
            EntryKind.HALT_CLEARED,
            {"halt_id": halt_id, "operator": operator, "note": note},
        )
    typer.secho(f"halt {halt_id} cleared by {operator}", fg=typer.colors.YELLOW)


@app.command()
def kill(path: str = "KILL", remove: bool = False) -> None:
    """Engage or release the kill switch by creating or deleting the kill file."""
    target = Path(path)
    if remove:
        if not target.exists():
            typer.echo(f"{path} does not exist; kill switch already clear")
            return
        target.unlink()
        typer.secho(f"kill switch released ({path} deleted)", fg=typer.colors.YELLOW)
        return
    target.write_text("engaged by pmx kill\n")
    typer.secho(f"KILL SWITCH ENGAGED ({path} created)", fg=typer.colors.RED)



@app.command()
def skew(venue: str = "kalshi", base_url: str = "") -> None:
    """Measure clock skew against a venue.

    Worth having its own command: both venues reject signatures whose timestamp has
    drifted, and the resulting 401 is indistinguishable from a bad credential. Checking
    skew first turns a confusing outage into a one-line diagnosis.
    """
    client = _market_data_client(venue, base_url)
    try:
        venue_time = client.server_time()
    finally:
        client.close()

    clock = Clock()
    try:
        measured = clock.observe_venue_time(venue, venue_time)
    except StaleTimestampError as exc:
        _fail(str(exc))
        return
    typer.echo(f"{venue} clock: {venue_time.isoformat()}")
    typer.secho(f"skew: {measured.total_seconds():+.3f}s", fg=typer.colors.GREEN)


@app.command()
def record(
    venue: str = "kalshi",
    market: str = typer.Option(..., help="Venue-native market key"),
    side: str = typer.Option("YES", help="Kalshi only: YES or NO"),
    seconds: int = typer.Option(60, help="How long to record"),
    interval: float = typer.Option(5.0, help="Seconds between polls"),
    out: str = "data/recorded",
    base_url: str = "",
) -> None:
    """Poll one market's book and append ticks to the parquet archive.

    Every day without recording is a day of backtest data that cannot be recovered, so
    this exists before any strategy needs it. Raw payloads are stored beside the parsed
    rows — the response schemas are unverified (docs/api-notes.md §0), and the raw bytes
    are what make a schema correction a reparse instead of a loss.
    """
    client = _market_data_client(venue, base_url)
    outcome = _outcome_ref(venue, client, market, side)

    deadline = time.monotonic() + seconds
    ticks = 0
    errors = 0
    try:
        with TickRecorder(out) as recorder:
            while time.monotonic() < deadline:
                if kill_switch_engaged("KILL"):
                    typer.secho("KILL file present: stopping", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=1)
                try:
                    quote = client.get_quote(outcome)
                except VenueError as exc:
                    # A read failure is not a reason to abandon the session, but it is
                    # never silent: recording gaps have to be visible in the summary.
                    errors += 1
                    typer.secho(f"read failed: {exc}", fg=typer.colors.YELLOW, err=True)
                else:
                    recorder.record(quote)
                    ticks += 1
                time.sleep(interval)
    finally:
        client.close()

    colour = typer.colors.GREEN if errors == 0 else typer.colors.YELLOW
    typer.secho(f"recorded {ticks} ticks ({errors} failed reads) to {out}", fg=colour)


def _market_data_client(venue: str, base_url: str) -> MarketDataVenue:
    if venue == "kalshi":
        return KalshiMarketData.public(base_url or PROD_BASE_URL)
    if venue == "polymarket":
        return PolymarketMarketData.public(base_url or CLOB_BASE_URL)
    _fail(f"unknown venue {venue!r}; expected 'kalshi' or 'polymarket'")
    raise AssertionError("unreachable")


def _outcome_ref(venue: str, client: MarketDataVenue, market: str, side: str) -> OutcomeRef:
    """Build the outcome reference, taking the event key from the venue rather than
    inventing one: an event key we made up would not aggregate correlated exposure."""
    resolved = client.get_market(market)
    if venue == "kalshi":
        return KalshiMarketData.outcome_ref(market, resolved.event_key, side)
    return PolymarketMarketData.outcome_ref(
        condition_id(market), token_id(side), resolved.event_key
    )



@app.command()
def digest(
    day: str = "",
    db: str = DEFAULT_DB,
    equity: str = "0",
    day_pnl: str = "0",
) -> None:
    """Print the daily digest, built from the audit ledger.

    Equity and P&L are supplied by the caller for now: position marking arrives with the
    portfolio tracker in Phase 3. Everything else — order counts, rejection reasons,
    halts, reconciliation status, ledger integrity — comes from the ledger, which is the
    record of what actually happened rather than what the running process believes.
    """
    audit_path = Path(db.replace(".db", "-audit.db"))
    if not audit_path.exists():
        _fail(f"no audit ledger at {audit_path}")
    target = date.fromisoformat(day) if day else utc_now().date()
    with Ledger(audit_path) as ledger:
        report = build_digest(
            ledger, target, equity=Usd(equity), day_pnl=Usd(day_pnl)
        )
    typer.echo(render_digest(report))
    if not report.ledger_verified:
        raise typer.Exit(code=1)


@app.command()
def recover(db: str = DEFAULT_DB) -> None:
    """Resolve every order whose state we do not know, against the venue.

    Run this before anything else after a crash. Until it reports safe, no order should
    be placed: an unresolved order means our position state is a guess, and every risk
    limit is computed from position state.
    """
    audit_path = Path(db.replace(".db", "-audit.db"))
    with OrderStore(db) as store, Ledger(audit_path) as ledger:
        pending = store.unresolved()
        if not pending:
            typer.secho("nothing to recover", fg=typer.colors.GREEN)
            return
        typer.echo(f"{len(pending)} order(s) in an unresolved state")
        # No venue clients are wired here yet: resolving requires authenticated trading
        # clients, which arrive with credentials. Listing them is still useful — it tells
        # the operator exactly what is outstanding before anything restarts.
        for record in pending:
            typer.secho(
                f"  {record.idempotency_key} {record.state} {record.venue} "
                f"{record.outcome_key} {record.quantity}@{record.limit_price}",
                fg=typer.colors.YELLOW,
            )
        ledger.append(
            EntryKind.RECONCILIATION,
            {"phase": "recover_cli", "unresolved": len(pending)},
        )
        typer.secho(
            "\nresolve these against the venue before trading resumes", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)


@app.command()
def heartbeat(path: str = "var/heartbeat", max_silence_seconds: int = 300) -> None:
    """Report whether the main loop is alive.

    A silently dead bot with open positions is worse than no bot: it has stopped
    trading, but it has also stopped cancelling, reconciling, and noticing.
    """
    status = Heartbeat(path, max_silence=timedelta(seconds=max_silence_seconds)).status()
    colour = typer.colors.GREEN if status.alive else typer.colors.RED
    typer.secho(status.detail, fg=colour)
    if not status.alive:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

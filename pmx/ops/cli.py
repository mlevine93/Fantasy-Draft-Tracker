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
from pathlib import Path

import typer

from pmx.audit.ledger import EntryKind, Ledger, LedgerIntegrityError
from pmx.risk.circuit import kill_switch_engaged
from pmx.risk.limits import ConfigError, load_risk_config
from pmx.risk.state import HaltStore

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


if __name__ == "__main__":
    app()

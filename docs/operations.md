# PMX — Operations (Phase 0)

## What exists

The risk engine and everything it needs, plus the audit ledger and the operator CLI.
No venue clients, no network calls, no code that can place an order. `pmx/execution/`
and `pmx/strategies/` do not exist yet; the AST test in
`tests/test_single_order_path.py` already enforces the rules they will have to follow.

## Setup

```bash
uv venv --python 3.11 .venv
uv pip install -e ".[dev]"
python scripts/secret_scan.py --install   # pre-commit secret scanner
cp .env.example .env                      # gitignored; fill in when Phase 1 starts
```

## Commands

```bash
pmx config              # validate risk_config.yaml and print what is in force
pmx halts               # active halts
pmx halts --history     # every halt ever tripped, and who cleared it
pmx clear-halt <id> --operator <name> --note "<why it is safe>"
pmx verify-ledger       # walk the hash chain end to end
pmx kill                # create the KILL file — stops everything
pmx kill --remove       # delete it
```

## The kill switch

`KILL` in the repo root. Its existence stops the system. It is checked before every
order and every loop iteration, no config can disable it, and `pmx kill` is a
convenience — `touch KILL` from any shell does exactly the same thing.

## Halts do not clear themselves

Every breaker is sticky and survives process restart, because restarting is the first
thing an operator does when a system stops trading. Clearing one requires an id, a name,
and a written reason, all of which land in the audit ledger.

## Checks

```bash
pytest                                        # full suite
pytest --cov=pmx.risk --cov-fail-under=100    # §9: 100% branch coverage on risk/
mypy pmx/
ruff check pmx/ scripts/ tests/
```

The coverage gate on `pmx/risk/` is 100% **branch** coverage and is not negotiable per
§9. If a branch is genuinely unreachable, delete it rather than excluding it — a
defensive branch no input can reach looks like a safety check while being dead code.

## Current state of the gates

| Gate | State |
| --- | --- |
| `live_trading_enabled` | **false** — stays false until Phase 3 |
| Kalshi fee model | **unverified** — every Kalshi signal is hard-rejected |
| Polymarket fee model | **unverified** — every Polymarket signal is hard-rejected |
| Strategy promotion | nothing is `LIVE`; no strategies exist yet |
| Reconciliation | never run; the engine rejects on that alone |

Four independent gates currently block every order. That is the intended Phase 0 state:
the engine is complete and the system is provably incapable of trading.

## Before Phase 3 (live capital)

See `docs/api-notes.md` §4 for the full list. The blocking item is that the venue
documentation hosts are unreachable from this environment, so the fee schedules cannot
be verified — and an unverified fee model is what turns a losing strategy into one that
reports a profit.

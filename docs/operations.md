# PMX — Operations (Phases 0–1)

## What exists

Phase 0: the risk engine and everything it needs, plus the audit ledger and the operator
CLI. Phase 1: read-only venue clients for Kalshi and Polymarket, the HTTP transport with
its rate limiter and retry policy, the canonical normalizer, and the tick recorder.

Still absent: `pmx/execution/` and `pmx/strategies/`. Nothing in the tree can place an
order, and the AST test in `tests/test_single_order_path.py` already enforces the rules
those packages will have to follow.

### Phase 1 caveat — response schemas are unverified

The venue documentation hosts are unreachable from this environment
(docs/api-notes.md §0), so the *response* shapes in `pmx/venues/` were written from SDK
source plus inference. Both clients carry `SCHEMA_VERIFIED = False`.

Three things make that survivable rather than dangerous:

1. Parsing is strict. A missing or unexpected field raises `VenueDataError` instead of
   defaulting, so a wrong guess fails on first contact.
2. The recorder stores the venue's raw payload beside every parsed row, so a schema
   correction is a reparse rather than a lost archive.
3. Nothing in Phase 1 touches money. A schema error here costs data quality, not capital.

Kalshi's *authentication* is a different matter: it is verified against Kalshi's own
published code and covered by a test that checks a real RSA-PSS signature.

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
pmx skew --venue kalshi # measure clock skew (check this first on any 401)
pmx record --venue kalshi --market <ticker> --side YES --seconds 3600
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
| Venue response schemas | unverified; both clients `SCHEMA_VERIFIED = False` |

Four independent gates currently block every order. That is the intended Phase 0 state:
the engine is complete and the system is provably incapable of trading.

## Before Phase 3 (live capital)

See `docs/api-notes.md` §4 for the full list. The blocking item is that the venue
documentation hosts are unreachable from this environment, so the fee schedules cannot
be verified — and an unverified fee model is what turns a losing strategy into one that
reports a profit.

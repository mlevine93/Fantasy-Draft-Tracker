# PMX — Operations (Phases 0–2)

## What exists

Phase 0: the risk engine and everything it needs, plus the audit ledger and the operator
CLI. Phase 1: read-only venue clients for Kalshi and Polymarket, the HTTP transport with
its rate limiter and retry policy, the canonical normalizer, and the tick recorder.
Phase 2: the execution router, the persistent order store, crash recovery, the
reconciler, the strategy interface, and the `manual` strategy.

The full path now exists end to end: operator thesis → `Signal` → risk engine →
`ProposedOrder` → router → venue. It has been exercised against a local fake venue that
injects the failures that actually cost money — a timeout after the venue accepted the
order, a crash between writing intent and reading the response, a position book that
disagrees with ours.

Kalshi's authenticated trading client now exists too — order placement, cancellation,
order lookup by our own idempotency key, positions and balance — plus the daily digest
and the heartbeat.

What does **not** exist: a Polymarket trading client (L1/L2 order signing), and any
verification of the Kalshi order payload. `pmx/venues/kalshi_trading.py` carries
`SCHEMA_VERIFIED = False` and a `require_verified_schema()` startup check that refuses to
come up live until one real demo order has confirmed the shapes.

### The three rules Phase 2 is built around

1. **Intent hits disk before the wire.** A crash after that leaves a row recovery can
   resolve; a crash before it means no order was sent.
2. **A timeout is not a rejection.** An ambiguous failure marks the order `UNKNOWN` and
   raises. Recovery asks the venue. Nothing is ever retried blind.
3. **The idempotency key is the primary key.** A second attempt at the same order is a
   database integrity error, not a second position.

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
pmx recover             # resolve orders in an unknown state — run first after a crash
pmx digest              # daily digest, built from the audit ledger
pmx heartbeat           # is the main loop alive?
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
| Kalshi order schema | unverified — `require_verified_schema()` blocks live startup |
| Polymarket trading client | not implemented (L1/L2 order signing) |
| Credentials | none present; `.env` is empty |
| Network egress to venues | blocked by environment policy (403 at the proxy) |

Every one of these independently blocks an order. The system is complete through the
router and provably incapable of trading.

## Before Phase 3 (live capital)

See `docs/api-notes.md` §4 for the full list. The blocking item is that the venue
documentation hosts are unreachable from this environment, so the fee schedules cannot
be verified — and an unverified fee model is what turns a losing strategy into one that
reports a profit.

# PMX — One-Page Spec (pre-Phase-0 approval gate)

**Date:** 2026-07-26. **Status:** awaiting operator approval. No code written yet.

## Scope

Risk-constrained, medium-frequency execution system for Kalshi and Polymarket. CLI only.
Python 3.11+, uv, `mypy --strict`, ruff, pytest, SQLite (WAL). Priority order:
capital safety > auditability > correctness > edge > latency > elegance.

## Module list

Layout as specified in the brief's §3, with these additions, each justified:

- `pmx/core/clock.py` — single source of skew-corrected UTC. Kalshi signs in **ms**,
  Polymarket in **seconds**; both venues expose server time. Callers must name the unit.
  Without this, clock skew is an intermittent, un-debuggable auth failure.
- `pmx/core/money.py` — `Probability`, `Usd`, `Cents`, `Contracts` as constrained Decimal
  newtypes. Arithmetic between mismatched units is a type error. This is how "no float in
  money math" gets enforced by the compiler rather than by vigilance.
- `pmx/core/ids.py` — `ConditionId`, `TokenId`, `MarketTicker`, `EventTicker`, `SeriesTicker`
  as distinct `NewType`s. Kills the condition-ID/token-ID class of bug at type-check time.
- `pmx/venues/fees.py` — fee schedules as **functions** with config-supplied coefficients and a
  `verified: bool` flag. Unverified fee model ⇒ risk engine rejects every signal on that venue.
- `pmx/risk/state.py` — the halt/position/equity state machine, persisted. Halts survive
  process restart; stickiness is a property of the database, not of a live object.

Everything else per brief. `risk/engine.py` exposes exactly one function capable of reaching a
venue's order path; a pytest that greps the AST for venue-client order-method calls outside
that module fails the build if anything else tries.

## Data model (canonical, all frozen pydantic)

```
Venue        = KALSHI | POLYMARKET
Market       (venue, venue_ids, question, outcomes[], close_time_utc,
              resolution_source, correlation_tags[], liquidity_snapshot, tick_size)
Outcome      (market_ref, side, venue_outcome_id)          # TokenId on PM, YES/NO on Kalshi
Quote        (outcome_ref, bid: Probability, ask: Probability, depth[], ts_utc,
              source: BOOK | INDICATIVE)                    # INDICATIVE is unsizeable, by type
Signal       (strategy, outcome_ref, direction, thesis_prob: Probability,
              limit_price: Probability, max_size, rationale: str, ts_utc, quote_ref)
Order        (idempotency_key, signal_ref, venue, outcome_ref, side, limit_price, qty,
              tif, state, venue_order_id?, ts_utc)
Fill         (order_ref, qty, price, fee_charged: Usd, fee_modeled: Usd, ts_utc)
Position     (outcome_ref, qty, avg_price, realized, unrealized, last_reconciled_utc)
LedgerEntry  (seq, ts_utc, kind, payload_json, prev_hash, hash)
```

Prices are `Probability` in [0,1] everywhere inside the boundary. `fee_charged` vs `fee_modeled`
on every Fill is what makes the fee model self-auditing: divergence beyond a cent halts.

## The five riskiest parts of this build, and how each is de-risked

1. **The fee/edge model is unverifiable right now** (see `docs/api-notes.md` §0). A wrong fee
   model manufactures phantom edge and the system trades it repeatedly with confidence.
   *De-risk:* fees are functions, not constants; coefficients default pessimistically high;
   `fee_model_verified: false` is a hard reject for that venue; every fill compares modeled vs
   charged fee and any divergence halts. The exchange validates our model on fill #1.

2. **State divergence between us and the venue** — a fill we didn't see, an order we think we
   cancelled, a process killed mid-placement. Every downstream limit is computed off local
   state, so divergence silently invalidates *all* of them at once.
   *De-risk:* idempotency key on every write; reconciler every 60s; pre-order reconciliation
   gate; crash-recovery path that on startup queries every non-terminal order's true state
   before doing anything else; the `kill -9` mid-placement test from §8 written in Phase 2.

3. **Correlation blindness.** Ten "Fed cuts in March" markets look like ten independent
   positions to a naive tracker and are one bet. This is the mechanism by which a system that
   respects every per-market limit still loses the whole bankroll on one event.
   *De-risk:* correlation tags are mandatory at normalization (Kalshi `event_ticker` /
   `series_ticker`; Polymarket `condition_id` and neg-risk group). Untagged market ⇒ treated as
   correlated with everything in its category, which is deliberately punitive so tagging gets
   done. Exposure limits evaluate on the tag aggregate, never the individual market.

4. **`cross_venue_arb` is not an arb.** Two questions that read identically can settle opposite
   ways — different resolution source, cutoff, void rules, UMA oracle discretion. Plus the
   collateral question from api-notes §2.3 (pUSD?) adds an unpriced conversion leg.
   *De-risk:* `verified_pairs` requires explicit one-time human confirmation and stores the exact
   resolution terms hash; any change to either side's terms auto-unmatches and flattens. Full
   cost stack modeled before the signal is emitted. **And: this strategy is built last, after
   the collateral question is resolved.** If the answer is "pUSD with a real conversion cost",
   I will tell you the strategy is probably not viable rather than ship it.

5. **The risk engine itself failing open.** Everything above assumes the gate holds. A bug, an
   uncaught exception, a config that silently parses to `None` and compares as "no limit" — any
   of these turn the whole system into an unbounded one.
   *De-risk:* built first, before any network code; tests written before implementation;
   100% branch coverage as a CI gate; hypothesis property tests asserting no sequence of valid
   inputs yields an order exceeding any limit; config schema is strict-validated with no
   defaults for any limit (missing limit = startup failure, never "unlimited"); every exception
   path in `risk/` is an explicit halt with no bare `except`.

## Phase 0 deliverable (what I build on your approval)

Repo scaffolding, `.gitignore` + pre-commit secret-scan hook, strict config loader,
hash-chained audit ledger, the money/id/clock primitives, the full risk engine with limits,
sizing (¼ Kelly, capped, logging raw vs capped), circuit breakers and sticky halts, and the
test suite for all of it. Zero network calls. Nothing in Phase 0 can place an order because
no venue client exists yet.

## Standing decisions (conservative choices made, not questions)

- Polymarket: `py-clob-client-v2==1.1.0` for signing only; reads over plain HTTP. (Rationale in
  api-notes §2.1 — the official unified SDK is 0.2.0 and reserves breaking minor releases.)
- Kalshi demo base URL: config value, probed at Phase 2 start, not hardcoded.
- Gamma-sourced prices are typed `INDICATIVE` and cannot reach a sizing calculation.
- PMX lives under `pmx/` in this repo alongside the existing unrelated `draft_tracker.py`.

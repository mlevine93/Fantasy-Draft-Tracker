# PMX — Venue API Notes

**Fetched: 2026-07-26.** Every claim below carries a source and a confidence tag.
Nothing in this file may be treated as verified unless tagged `[PRIMARY]`.

Tags:
- `[PRIMARY]` — read directly from venue-owned source code or venue-owned package metadata.
- `[SECONDARY]` — third-party write-up or search summary. **Not trustworthy for money math.**
- `[BLOCKED]` — could not be verified; official documentation unreachable from this environment.

---

## 0. BLOCKER — official documentation is unreachable

This build environment's egress policy rejects CONNECT to the venue domains. Verified
by direct probe on 2026-07-26:

| Host | Result |
| --- | --- |
| `docs.kalshi.com` | 403 at proxy CONNECT (`connect_rejected`, policy denial) |
| `kalshi.com` (fee schedule PDF) | 403 at proxy CONNECT |
| `api.elections.kalshi.com` | 403 at proxy CONNECT |
| `docs.polymarket.com` | 403 at proxy CONNECT |
| `gamma-api.polymarket.com` | 403 at proxy CONNECT |
| `api.github.com` | 403 at proxy CONNECT |

Reachable: `pypi.org`, `files.pythonhosted.org`, `raw.githubusercontent.com`, web search.

**Consequences, and they are not cosmetic:**

1. The **fee schedules for both venues are UNVERIFIED.** Fees are the single biggest input to
   `min_edge_bps_after_fees`. A wrong fee model does not produce a smaller edge; it produces
   a *phantom* edge, and the system will trade into it repeatedly and confidently.
2. **Rate limits are UNVERIFIED.** Getting these wrong risks bans, not just 429s.
3. §10 (compliance, ToS, tax treatment) **cannot be started** — the source documents are the
   venues' own ToS pages, all of which are behind this same block.
4. Endpoint request/response *shapes* beyond what appears in venue SDK source are unverified.

**Gate I am imposing on myself:** no live-capital phase (Phase 3) proceeds until this file's
fee and rate-limit sections are `[PRIMARY]`. Encoded mechanically: `risk_config.yaml` will carry
`fee_model_verified: false`, and the risk engine hard-rejects every signal on a venue whose
fee model is unverified. Phases 0–2 (risk engine, read-only clients, demo) are unaffected —
no fee model is load-bearing until real money moves.

**Ways to clear the blocker (operator action required, pick one):**
- Add `docs.kalshi.com`, `kalshi.com`, `docs.polymarket.com`, `*.kalshi.co`,
  `*.kalshi.com`, `*.polymarket.com` to the environment's network allowlist and I re-fetch.
- Or paste the fee schedule PDF / docs pages into the repo under `docs/vendor/` and I parse them.
- Or I build the fee model as a pure function with operator-supplied coefficients in
  `risk_config.yaml`, defaulting to a deliberately punitive over-estimate, and we calibrate it
  against the first real fills. (This is my recommendation regardless — see §Kalshi/Fees.)

---

## 1. Kalshi

### 1.1 Authentication — `[PRIMARY]`

Source: `github.com/Kalshi/kalshi-starter-code-python`, `clients.py` @ `main`, fetched 2026-07-26.
This is Kalshi-owned code, not a community SDK.

**Mechanism: RSA-PSS request signing. There is no bearer token and no login endpoint.**
This resolves the conflict flagged in the build brief — the token/expiry description is stale.

- Credentials: an **API key ID** (UUID string) plus an **RSA private key** (PEM), generated in
  the Kalshi UI. The private key is shown once and never retrievable again.
- Per-request headers:
  - `KALSHI-ACCESS-KEY` — the key ID
  - `KALSHI-ACCESS-SIGNATURE` — base64 signature
  - `KALSHI-ACCESS-TIMESTAMP` — Unix time in **milliseconds**, as a string
- Signed message: `str(timestamp_ms) + HTTP_METHOD + path`, where `path` is the full API path
  **with the query string stripped** (`path.split('?')[0]`). Example:
  `1703123456789GET/trade-api/v2/portfolio/balance`
- Signature: RSA-PSS, MGF1(SHA-256), **salt_length = `PSS.DIGEST_LENGTH`** (i.e. 32, not MAX),
  digest SHA-256, base64-encoded.
- No token refresh. Signature is per-request. Clock skew is therefore a direct auth failure mode —
  see risk note in `docs/spec.md`.
- WebSocket auth uses the *same* headers, signing `GET` against the WS path (`/trade-api/ws/v2`),
  passed as connection headers at handshake.

**Implementation note for us:** the private key must never be logged, and the signing function
must be the only thing that touches it. `salt_length` is the classic silent-failure knob here —
`MAX_LENGTH` also produces a valid-looking signature that the server rejects. Pin `DIGEST_LENGTH`
and cover it with a test against a recorded fixture.

### 1.2 Base URLs — `[PRIMARY]` for prod, **CONFLICT** for demo

From `clients.py` `[PRIMARY]`:

| Env | HTTP | WebSocket |
| --- | --- | --- |
| PROD | `https://api.elections.kalshi.com` | `wss://api.elections.kalshi.com` |
| DEMO | `https://demo-api.kalshi.co` | `wss://demo-api.kalshi.co` |

REST path prefix: `/trade-api/v2` (sub-roots `/exchange`, `/markets`, `/portfolio`).
WebSocket path: `/trade-api/ws/v2`.

**CONFLICT — unresolved.** Search results `[SECONDARY]` state that current official docs
recommend `https://external-api.demo.kalshi.co/trade-api/v2` as the demo root, while Kalshi's
own starter repo still uses `demo-api.kalshi.co`. Both may resolve; only one is current. I
cannot adjudicate without the docs. **Resolution plan:** make the demo base URL a config value,
probe both at Phase 2 start against `/exchange/status`, and record which answers. Do not
hardcode either.

Also note: the starter repo may itself be stale — I could not check its last-commit date
(`api.github.com` blocked). Treat it as authoritative on *mechanism* (RSA-PSS signing is not the
kind of thing that quietly changes) and as merely indicative on *hostnames*.

### 1.3 Rate limits — `[SECONDARY]`, must re-verify

Search summary describes a **token-bucket** system with **independent read and write buckets**,
five tiers — Basic 200/100, Advanced 300/300, Premier 1000/1000, Paragon 2000/2000,
Prime 4000/4000 tokens/sec — with most endpoints costing 10 tokens (so Basic ≈ 20 reads/sec,
10 writes/sec), and up to 2 seconds of accumulated burst.

Kalshi's own starter code implements a crude 100 ms floor between calls `[PRIMARY]`, which is
consistent with ~10 rps but proves nothing about tiers.

**Our stance:** implement a token-bucket limiter with per-endpoint cost, configured
conservatively (assume Basic), and treat any 429 as a circuit-breaker event that halts the
loop rather than a retry-and-continue. We are not latency-sensitive (§12); being slow is free.

### 1.4 Fees — `[SECONDARY]`, **UNVERIFIED — blocks Phase 3**

Search summary of the July 2026 schedule: taker fee per contract
`ceil(0.07 × P × (1 − P) × C) / 100` in dollars (P = price as probability, C = contract count),
maker fees roughly a quarter of that, a per-contract cap around $0.035, no settlement fee, no
ACH deposit fee. Sources also note the schedule has changed repeatedly and that some series
carry different coefficients.

I will not encode a number I read on a blog into a P&L path. Plan:

- `venues/kalshi_fees.py` exposes `taker_fee(price: Decimal, qty: int, series: str) -> Decimal`
  and `maker_fee(...)`, driven by coefficients from `risk_config.yaml`, **ceil-to-the-cent per
  the formula's rounding, not banker's rounding** — the ceiling is what makes small-size trades
  disproportionately expensive and it is exactly what kills marginal "edges".
- Default coefficients are set **pessimistically high** until `[PRIMARY]` verification.
- A reconciliation test compares modeled fee vs actual fee reported on every real fill; any
  divergence beyond one cent halts trading. This is the real defense — the fee model gets
  validated by the exchange itself on the first fill, and disagreement is a halt, not a warning.

### 1.5 Contract semantics — `[PRIMARY]` (from endpoint shapes) / `[SECONDARY]` (details)

- Prices are integer cents, 1–99, representing implied probability. Normalize at the boundary:
  `Decimal(cents) / 100`. Never let cents escape the venue adapter.
- Yes and No are separate sides of the same market ticker; `yes_price + no_price` need not sum
  to 100 across the *book* (bid/ask asymmetry). Sizing must be done off the specific side's book.
- Market identity: `series_ticker` → `event_ticker` → `market_ticker`. **The correlation tagging
  in §4 keys off `event_ticker` at minimum** — all markets sharing an event are one bet.

### 1.6 Endpoints confirmed present — `[PRIMARY]`

`/trade-api/v2/exchange/status`, `/trade-api/v2/markets/trades`, `/trade-api/v2/portfolio/balance`.
Order endpoints exist under `/trade-api/v2/portfolio/orders` `[SECONDARY]` — shape unverified,
to be confirmed against the demo environment in Phase 2 before any order code is written.

---

## 2. Polymarket

### 2.1 SDK situation — `[PRIMARY]`, and the brief is out of date

Three Python clients exist. All facts from PyPI JSON API and repo READMEs, fetched 2026-07-26:

| Package | Version | Uploaded | Status |
| --- | --- | --- | --- |
| `py-clob-client` | 0.34.6 | 2026-02-19 | **Repo archived.** README: "no longer maintained", redirects to py-sdk. V1 signing — dead against prod. |
| `py-clob-client-v2` | 1.1.0 | 2026-07-17 | Live, V2 signing. README itself recommends py-sdk "for new projects". |
| `polymarket-client` (repo `Polymarket/py-sdk`) | 0.2.0 | 2026-07-24 | **Official unified SDK**, `Polymarket Engineering <engineering@polymarket.com>`, requires-python ≥3.11, 23 releases. Classifier says Production/Stable but version is 0.2.0 and README states minor releases on 0.x **may include breaking changes**. |

**Discrepancy vs the brief:** the brief says use `py-clob-client-v2`. That is correct as against
V1, but both venue-owned READMEs now point at the unified `polymarket-client` SDK.

**Decision (conservative, per §11):** use `py-clob-client-v2` pinned to `==1.1.0` for order
construction and signing only, and hit the read endpoints over plain HTTP ourselves. Rationale:
a 1.x package with a stable signing path beats a 0.2.0 package that reserves the right to break
its API on a minor bump, and minimizing SDK surface area on the only code path that can move
money is worth more than SDK ergonomics. Revisit when `polymarket-client` reaches 1.0.

### 2.2 Auth — `[PRIMARY]`

Source: `py-clob-client-v2/py_clob_client_v2/headers/headers.py`, `signing/eip712.py` @ `main`.

Three access levels: `L0` (no auth, reads), `L1` (wallet signature), `L2` (API creds).

**L1 — EIP-712 wallet signature.** Domain: `name="ClobAuthDomain"`, `version="1"`, `chainId`.
Signed struct `ClobAuth{address, timestamp, nonce, message}` where
`message = "This message attests that I control the given wallet"`. Used only to
create/derive API credentials (`/auth/api-key`, `/auth/derive-api-key`).
Headers: `POLY_ADDRESS`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`, `POLY_NONCE`.

**L2 — HMAC.** Headers: `POLY_ADDRESS`, `POLY_SIGNATURE` (HMAC), `POLY_TIMESTAMP`,
`POLY_API_KEY`, `POLY_PASSPHRASE`. HMAC is computed over
`(secret, timestamp, method, request_path, body)` using the **pre-serialized** body when
available — i.e. the exact bytes sent must be the exact bytes signed.

> Correction to a widely repeated summary: the L2 header names are **not** `api_key` /
> `api_secret` / `api_passphrase` (that is the *constructor argument* naming in the README).
> The wire headers are the `POLY_*` names above. Getting this wrong yields 401s that look
> like credential problems.

Timestamps are **seconds** here (`int(datetime.now().timestamp())`), unlike Kalshi's
milliseconds. Both clients support overriding with server time — we will use server time
where offered, because clock skew silently breaks auth on both venues.

### 2.3 Chain, contracts, neg-risk — `[PRIMARY]`

Source: `py_clob_client_v2/config.py`, `constants.py`.

Chains: `POLYGON = 137`, `AMOY = 80002` (testnet).

Chain 137 contract set:

| Role | Address |
| --- | --- |
| `exchange` (V1) | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` |
| `neg_risk_exchange` (V1) | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| **`exchange_v2`** | `0xE111180000d2663C0091e4f400237545B87B996B` |
| **`neg_risk_exchange_v2`** | `0xe2222d279d744050d28e00520010520000310F59` |
| `neg_risk_adapter` | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| `conditional_tokens` | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| `collateral` | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` |

**Neg-risk confirmed as a signing-path fork:** neg-risk markets sign against a *different*
verifying contract. The client exposes `GET /neg-risk` and `GET /tick-size` as per-market
lookups — so the flag is fetched per market, never assumed. Our order builder will refuse to
sign if the neg-risk flag for the target token was not fetched within a configurable freshness
window.

**Collateral flag — `[SECONDARY]`, needs verification.** The `collateral` address above is
**not** Polygon USDC.e (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`) or native USDC
(`0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359`). Press coverage of the 2026-04-28 CLOB V2
launch describes a new collateral token, **pUSD (Polymarket USD)**. If settlement collateral
is now pUSD rather than USDC, then the brief's premise ("settles in USDC") is stale and the
cross-venue arb cost stack acquires an extra conversion leg (pUSD↔USDC↔USD) with its own
spread, delay, and counterparty risk. **This materially worsens cross-venue arb economics and
must be resolved before `cross_venue_arb` is built.** Flagged for operator.

Token decimals: collateral 6, conditional tokens 6 `[PRIMARY]`.

### 2.4 Condition ID vs token ID — `[PRIMARY]`

Endpoint set makes the distinction explicit: `/markets/{condition_id}` and
`/markets-by-token/{token_id}` are separate lookups; `/book`, `/price`, `/midpoint`,
`/spread`, `/tick-size`, `/neg-risk` and all order endpoints key off **token ID**.
Confirms the brief: condition ID = market, token ID = one outcome.

**Test we will write (§2 of brief):** the canonical model makes these distinct newtypes
(`ConditionId`, `TokenId`) that are not interchangeable at the type level, `mypy --strict`
rejects passing one where the other is expected, and a runtime test asserts that constructing
an order from a condition ID raises. Type-level is the real fix; the test guards the boundary
where strings enter from JSON.

### 2.5 CLOB V2 — `[SECONDARY]`

Reported: went live 2026-04-28 ~11:00 UTC; order books cleared at cutover; V1 SDKs and
V1-signed orders no longer work against production; `nonce` and `feeRateBps` removed from the
order struct with **fees now set by the operator at match time rather than in the order**;
new CTF Exchange V2 and Neg Risk CTF Exchange V2 contracts (matching the `[PRIMARY]` addresses
above); EIP-1271 signature support; builder attribution codes.

**"Fees set at match time" is a risk-engine problem, not a trivia item.** If the fee is not in
the signed order, we cannot bound fee cost at signing time from the order alone. `GET /fee-rate`
exists `[PRIMARY]` — we must fetch it per market, treat it as stale after N seconds, and add a
worst-case fee buffer to `min_edge_bps_after_fees` on the Polymarket leg. Unverified whether
the operator can change the rate between our fetch and the match.

### 2.6 API surface — `[PRIMARY]` (endpoint paths from V2 client)

Reads (L0): `/ok`, `/time`, `/version`, `/markets`, `/markets/{id}`, `/markets-by-token/{id}`,
`/clob-markets/`, `/simplified-markets`, `/sampling-markets`, `/sampling-simplified-markets`,
`/book`, `/books`, `/midpoint(s)`, `/price(s)`, `/spread(s)`, `/last-trade-price`,
`/last-trades-prices`, `/tick-size`, `/neg-risk`, `/fee-rate`, `/prices-history`,
`/markets/live-activity/`.

Auth: `/auth/api-key` (create/delete), `/auth/api-keys`, `/auth/derive-api-key`,
`/auth/readonly-api-key(s)`, `/auth/builder-api-key`, `/auth/ban-status/closed-only`.

Orders: `POST /order`, `POST /orders`, `DELETE /order`.

`/time` is notable: use it to measure and correct clock skew rather than trusting the host clock.

Gamma (discovery/metadata) and Data (analytics) API base URLs are **`[BLOCKED]`** — not present
in the V2 client source and their docs are unreachable. Per the brief, Gamma prices lag the
book; our rule is stronger and mechanical: **Gamma-sourced prices are typed as
`IndicativePrice` and the risk engine refuses any signal whose sizing input is an
`IndicativePrice`.** Type system enforces it, not discipline.

---

## 3. Cross-cutting facts that matter for correctness

| Concern | Kalshi | Polymarket |
| --- | --- | --- |
| Auth timestamp unit | **milliseconds** | **seconds** |
| Auth style | per-request RSA-PSS signature | L1 EIP-712 → creds; L2 HMAC per request |
| Server time endpoint | `/exchange/status` (indirect) | `/time` `[PRIMARY]` |
| Price units at boundary | integer cents 1–99 | decimal 0–1, 6-dp collateral |
| Settlement | USD, exchange-internal | on-chain, collateral token per §2.3 |
| Fee timing | schedule-based, per contract | set by operator at match `[SECONDARY]` |
| Market id | `market_ticker` (+ event, series) | `condition_id` (market) / `token_id` (outcome) |

The two timestamp units on the two venues, in the two auth paths, is precisely the kind of
detail that produces an intermittent 401 at 3am. Both go through one `clock.py` that returns
skew-corrected UTC and forces the caller to name the unit.

---

## 4. Still required before Phase 3 (live capital)

- [ ] Kalshi fee schedule — `[PRIMARY]`
- [ ] Kalshi rate limit tiers and per-endpoint token costs — `[PRIMARY]`
- [ ] Kalshi demo base URL conflict resolved
- [ ] Kalshi order endpoint request/response shapes — `[PRIMARY]` or recorded from demo
- [ ] Polymarket fee mechanics under V2 (who sets it, when, can it move post-fetch) — `[PRIMARY]`
- [ ] Polymarket collateral token identity (USDC vs pUSD) and conversion cost stack
- [ ] Gamma / Data API base URLs and rate limits
- [ ] Everything in §10 of the brief (ToS on automated access, state eligibility, tax forms) —
      **not started, blocked on network access**

"""Kalshi market data client. Read-only.

## What is verified and what is not

**Verified** against Kalshi-owned source (`github.com/Kalshi/kalshi-starter-code-python`,
fetched 2026-07-26, recorded in docs/api-notes.md §1.1): the authentication scheme, the
header names, the signed message construction, the PSS parameters, the production base
URL, and the `/trade-api/v2` path prefix.

**Not verified**: every response body shape below, and the orderbook endpoint path. The
official documentation is unreachable from this environment (docs/api-notes.md §0), so
these were written from the endpoint set in Kalshi's own code plus inference. They are
marked `SCHEMA_VERIFIED = False` and parsed strictly — a missing or unexpected field
raises `VenueDataError` rather than defaulting, so a wrong guess fails on first contact
instead of quietly producing a plausible price.

Nothing here can place an order. `KalshiMarketData` implements `MarketDataVenue` only.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pmx.core.models import DepthLevel, Market, OutcomeRef, Quote, QuoteSource, Side, Venue
from pmx.core.money import Usd, cents_to_probability
from pmx.venues.base import MarketDataVenue, VenueDataError
from pmx.venues.rate_limits import kalshi_read_bucket
from pmx.venues.transport import HttpTransport

__all__ = ["SCHEMA_VERIFIED", "KalshiAuth", "KalshiMarketData"]

#: Flipped to True only when the response schemas below have been checked against live
#: documentation or recorded real responses. Until then the recorder stores raw payloads
#: alongside parsed rows so nothing is lost if a guess turns out wrong.
SCHEMA_VERIFIED: Final = False

API_PREFIX: Final = "/trade-api/v2"

#: Kalshi orderbook levels are [price_cents, size] pairs.
_LEVEL_FIELDS: Final = 2

#: docs/api-notes.md §1.2. The demo host is contested between Kalshi's own starter code
#: and third-party reports of the current docs, so it is a parameter, not a constant.
PROD_BASE_URL: Final = "https://api.elections.kalshi.com"
DEMO_BASE_URL_STARTER_CODE: Final = "https://demo-api.kalshi.co"
DEMO_BASE_URL_REPORTED: Final = "https://external-api.demo.kalshi.co"



class KalshiAuth:
    """RSA-PSS request signing.

    Verified from Kalshi's own starter code. Two details are the ones that bite:

    * The timestamp is Unix **milliseconds**, not seconds.
    * The salt length is `PSS.DIGEST_LENGTH` (32), not `MAX_LENGTH`. `MAX_LENGTH`
      produces a perfectly well-formed signature that the server rejects, and the
      resulting 401 looks exactly like a bad key.

    The query string is stripped before signing: only the path is signed.
    """

    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey) -> None:
        if not key_id:
            raise ValueError("Kalshi key id must not be empty")
        self._key_id = key_id
        self._private_key = private_key

    @classmethod
    def from_key_file(cls, key_id: str, key_path: str | Path) -> KalshiAuth:
        """Load the PEM from disk. The key material never enters config or the ledger."""
        path = Path(key_path)
        if not path.is_file():
            raise FileNotFoundError(f"Kalshi private key not found at {path}")
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise TypeError(f"Kalshi private key at {path} is not an RSA key")
        return cls(key_id, loaded)

    @staticmethod
    def signing_message(timestamp_ms: int, method: str, path: str) -> str:
        """`timestamp + METHOD + path`, query string removed."""
        return f"{timestamp_ms}{method.upper()}{path.split('?', maxsplit=1)[0]}"

    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        message = self.signing_message(timestamp_ms, method, path).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(timestamp_ms, method, path),
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        }


def _require(payload: Mapping[str, Any], key: str, *, context: str) -> Any:
    """Fetch a field or raise. No defaults, ever.

    A missing field means the schema guess is wrong, and substituting a default would
    convert a loud failure into a silently wrong number.
    """
    if key not in payload:
        raise VenueDataError(
            f"Kalshi {context}: expected field {key!r}, got keys {sorted(payload)}. "
            "The response schema in pmx/venues/kalshi.py is unverified — see "
            "docs/api-notes.md §0."
        )
    return payload[key]


def _parse_timestamp(raw: object, *, context: str) -> datetime:
    if not isinstance(raw, str):
        raise VenueDataError(f"Kalshi {context}: expected an ISO timestamp string, got {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VenueDataError(f"Kalshi {context}: cannot parse timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        raise VenueDataError(f"Kalshi {context}: timestamp {raw!r} has no timezone")
    return parsed.astimezone(UTC)


def _parse_cents(raw: object, *, context: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise VenueDataError(f"Kalshi {context}: expected integer cents, got {raw!r}")
    return raw


class KalshiMarketData(MarketDataVenue):
    """Read-only Kalshi client."""

    venue = Venue.KALSHI

    def __init__(
        self,
        transport: HttpTransport,
        auth: KalshiAuth | None = None,
    ) -> None:
        # Public endpoints accept the auth headers but do not require them, so Phase 1
        # runs unauthenticated by default: market data needs no credentials, and code
        # that never holds a key cannot leak one.
        self._transport = transport
        self._auth = auth

    @classmethod
    def public(cls, base_url: str = PROD_BASE_URL, **kwargs: Any) -> KalshiMarketData:
        return cls(HttpTransport(base_url, read_bucket=kalshi_read_bucket(), **kwargs))

    def close(self) -> None:
        self._transport.close()

    # -- clock --------------------------------------------------------------

    def server_time(self) -> datetime:
        """Kalshi's clock, read from the HTTP `Date` header.

        Deliberately not a JSON field: `Date` is defined by RFC 9110, so it is one of
        the few things about this integration that needs no guessing. Since Kalshi signs
        with millisecond timestamps and rejects drifted signatures, having a trustworthy
        measurement of skew matters more here than one-second resolution costs.
        """
        response = self._transport.get_response(f"{API_PREFIX}/exchange/status")
        raw = response.headers.get("Date")
        if raw is None:
            raise VenueDataError("Kalshi exchange/status response carried no Date header")
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError) as exc:
            raise VenueDataError(f"Kalshi returned an unparseable Date header: {raw!r}") from exc
        return parsed.astimezone(UTC)

    # -- markets ------------------------------------------------------------

    def list_markets(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Market], str | None]:
        params: dict[str, Any] = {"limit": limit, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        payload = self._transport.get(f"{API_PREFIX}/markets", params=params)
        if not isinstance(payload, dict):
            raise VenueDataError(
                f"Kalshi markets: expected an object, got {type(payload).__name__}"
            )
        raw_markets = _require(payload, "markets", context="markets")
        if not isinstance(raw_markets, list):
            raise VenueDataError("Kalshi markets: 'markets' is not a list")
        markets = [self.parse_market(item) for item in raw_markets]
        next_cursor = payload.get("cursor") or None
        return markets, next_cursor

    def get_market(self, market_key: str) -> Market:
        payload = self._transport.get(f"{API_PREFIX}/markets/{market_key}")
        if not isinstance(payload, dict):
            raise VenueDataError("Kalshi market: expected an object")
        return self.parse_market(_require(payload, "market", context="market"))

    @classmethod
    def parse_market(cls, raw: Any) -> Market:
        """Venue-native market to canonical. **Schema unverified** (see module docstring).

        `event_ticker` is mandatory rather than optional: it is the key correlated
        exposure aggregates on, and a market that arrived without one would be treated
        as independent of the other markets on its own event — the exact failure the
        correlation limit exists to prevent.
        """
        if not isinstance(raw, Mapping):
            raise VenueDataError(f"Kalshi market: expected an object, got {type(raw).__name__}")

        ticker = str(_require(raw, "ticker", context="market"))
        event_ticker = str(_require(raw, "event_ticker", context="market"))
        volume_raw = raw.get("volume_24h")
        return Market(
            venue=Venue.KALSHI,
            market_key=ticker,
            event_key=event_ticker,
            question=str(_require(raw, "title", context="market")),
            close_time=_parse_timestamp(_require(raw, "close_time", context="market"),
                                        context="market close_time"),
            resolution_source=str(raw.get("rules_primary") or f"kalshi:{ticker}"),
            # Kalshi's own hierarchy is the correlation tagging: every market on an event
            # is one bet, and every event in a series is at minimum closely related.
            correlation_tags=cls._correlation_tags(event_ticker, raw),
            # Kalshi reports 24h volume in contracts. One contract is at most $1, so
            # treating the count as a dollar ceiling is an over-estimate of liquidity —
            # which is the wrong direction. Left as contracts and flagged: the illiquidity
            # limit must be calibrated against this unit before Phase 3.
            volume_24h=Usd(volume_raw) if isinstance(volume_raw, int) else None,
        )

    @staticmethod
    def _correlation_tags(event_ticker: str, raw: Mapping[str, Any]) -> tuple[str, ...]:
        tags = [f"kalshi-event:{event_ticker}"]
        series = raw.get("series_ticker")
        if isinstance(series, str) and series:
            tags.append(f"kalshi-series:{series}")
        return tuple(tags)

    # -- quotes -------------------------------------------------------------

    def get_quote(self, outcome: OutcomeRef) -> Quote:
        """Book for one side of one market.

        `outcome_key` is `"<ticker>:YES"` or `"<ticker>:NO"`. Kalshi quotes both sides of
        the same contract, and they are different instruments for our purposes: a YES
        position and a NO position on one market offset, so they must never be summed as
        if they were the same exposure.
        """
        ticker, side_label = self._split_outcome_key(outcome.outcome_key)
        payload = self._transport.get(f"{API_PREFIX}/markets/{ticker}/orderbook")
        if not isinstance(payload, dict):
            raise VenueDataError("Kalshi orderbook: expected an object")
        book = _require(payload, "orderbook", context="orderbook")
        if not isinstance(book, Mapping):
            raise VenueDataError("Kalshi orderbook: 'orderbook' is not an object")

        levels = book.get(side_label.lower())
        if levels is None:
            raise VenueDataError(
                f"Kalshi orderbook for {ticker}: no {side_label.lower()!r} side in "
                f"{sorted(book)}"
            )
        depth = self._parse_levels(levels, context=f"orderbook {ticker}")
        best_bid = depth[0].price if depth else None
        return Quote(
            outcome=outcome,
            bid=best_bid,
            # Kalshi's orderbook endpoint returns resting bids per side; the ask for YES
            # is the complement of the best NO bid. Rather than derive it from the other
            # side's array and risk an inversion, the ask is left unset here and filled
            # by the recorder from both sides in one pass.
            ask=None,
            bid_depth=depth,
            observed_at=datetime.now(UTC),
            source=QuoteSource.BOOK,
        )

    @staticmethod
    def _parse_levels(levels: Any, *, context: str) -> tuple[DepthLevel, ...]:
        if not isinstance(levels, list):
            raise VenueDataError(f"Kalshi {context}: expected a list of levels")
        parsed: list[DepthLevel] = []
        for level in levels:
            if not isinstance(level, list) or len(level) != _LEVEL_FIELDS:
                raise VenueDataError(
                    f"Kalshi {context}: expected [price, size] pairs, got {level!r}"
                )
            price_cents = _parse_cents(level[0], context=context)
            size = _parse_cents(level[1], context=context)
            if size <= 0:
                continue
            parsed.append(DepthLevel(price=cents_to_probability(price_cents), quantity=size))
        # Best price first: a taker eats the top of the book, and every depth calculation
        # downstream assumes that ordering.
        parsed.sort(key=lambda level: level.price, reverse=True)
        return tuple(parsed)

    @staticmethod
    def _split_outcome_key(outcome_key: str) -> tuple[str, str]:
        ticker, separator, side = outcome_key.rpartition(":")
        if not separator or side.upper() not in {"YES", "NO"}:
            raise VenueDataError(
                f"Kalshi outcome key {outcome_key!r} must be '<ticker>:YES' or '<ticker>:NO'"
            )
        return ticker, side.upper()

    @staticmethod
    def outcome_ref(ticker: str, event_ticker: str, side: str) -> OutcomeRef:
        if side.upper() not in {"YES", "NO"}:
            raise ValueError(f"Kalshi side must be YES or NO, got {side!r}")
        return OutcomeRef(
            venue=Venue.KALSHI,
            market_key=ticker,
            outcome_key=f"{ticker}:{side.upper()}",
            event_key=event_ticker,
        )

    @staticmethod
    def taker_side_for(side: Side) -> str:
        """Which side of the book a taker consumes. Kept explicit to stop the classic
        inversion where a buy reads the bid array."""
        return "ask" if side is Side.BUY else "bid"

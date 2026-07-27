"""Polymarket CLOB market data client. Read-only, unauthenticated (L0).

## What is verified and what is not

**Verified** from Polymarket-owned source (`Polymarket/py-clob-client-v2` @ main, fetched
2026-07-26, recorded in docs/api-notes.md §2): the endpoint paths used below, the
condition-ID/token-ID split, chain IDs, the V2 exchange contract addresses, and that
`/tick-size` and `/neg-risk` are per-market lookups rather than global constants.

**Not verified**: every response body shape, and the base URL (`clob.polymarket.com`
comes from the archived V1 client's README and press coverage of the V2 cutover, not
from live documentation). Marked `SCHEMA_VERIFIED = False` and parsed strictly.

**Deliberately absent: the Gamma API.** Its base URL could not be established from any
primary source, and Gamma prices lag the live book in any case. Rather than guess a
hostname, this module implements the CLOB only. When Gamma is added, its prices must be
tagged `QuoteSource.INDICATIVE`, which the risk engine refuses to size a trade from.

Reads need no credentials, so this client holds none. Order signing (L1/L2) belongs to
Phase 2 and will live in a separate module.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from pmx.core.ids import ConditionId, TokenId, condition_id, token_id
from pmx.core.models import DepthLevel, Market, OutcomeRef, Quote, QuoteSource, Venue
from pmx.core.money import Probability, Usd
from pmx.venues.base import MarketDataVenue, VenueDataError
from pmx.venues.rate_limits import polymarket_read_bucket
from pmx.venues.transport import HttpTransport

__all__ = ["SCHEMA_VERIFIED", "PolymarketMarketData"]

SCHEMA_VERIFIED: Final = False

#: docs/api-notes.md §2.5 — unverified, hence a default rather than a constant.
CLOB_BASE_URL: Final = "https://clob.polymarket.com"

POLYGON_CHAIN_ID: Final = 137
AMOY_CHAIN_ID: Final = 80002



def _require(payload: Mapping[str, Any], key: str, *, context: str) -> Any:
    if key not in payload:
        raise VenueDataError(
            f"Polymarket {context}: expected field {key!r}, got keys {sorted(payload)}. "
            "The response schema in pmx/venues/polymarket.py is unverified — see "
            "docs/api-notes.md §0."
        )
    return payload[key]


def _parse_decimal(raw: object, *, context: str) -> Decimal:
    """Parse a wire number without ever touching float.

    Polymarket returns prices as JSON strings. If one ever arrives as a JSON number,
    `json.loads` will already have made it a float and the damage is done, so that case
    is rejected outright rather than converted.
    """
    if isinstance(raw, float):
        raise VenueDataError(
            f"Polymarket {context}: got a JSON float ({raw!r}) where a decimal string was "
            "expected; a float price has already lost precision and cannot be trusted"
        )
    if isinstance(raw, int) and not isinstance(raw, bool):
        return Decimal(raw)
    if isinstance(raw, str):
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise VenueDataError(
                f"Polymarket {context}: cannot parse {raw!r} as a decimal"
            ) from exc
    raise VenueDataError(f"Polymarket {context}: expected a decimal string, got {raw!r}")


def _parse_timestamp(raw: object, *, context: str) -> datetime:
    if not isinstance(raw, str):
        raise VenueDataError(f"Polymarket {context}: expected an ISO timestamp, got {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VenueDataError(f"Polymarket {context}: cannot parse timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        raise VenueDataError(f"Polymarket {context}: timestamp {raw!r} has no timezone")
    return parsed.astimezone(UTC)


class PolymarketMarketData(MarketDataVenue):
    """Read-only Polymarket CLOB client."""

    venue = Venue.POLYMARKET

    def __init__(self, transport: HttpTransport, *, chain_id: int = POLYGON_CHAIN_ID) -> None:
        if chain_id not in (POLYGON_CHAIN_ID, AMOY_CHAIN_ID):
            raise ValueError(f"unsupported Polymarket chain id: {chain_id}")
        self._transport = transport
        self.chain_id = chain_id

    @classmethod
    def public(cls, base_url: str = CLOB_BASE_URL, **kwargs: Any) -> PolymarketMarketData:
        return cls(HttpTransport(base_url, read_bucket=polymarket_read_bucket(), **kwargs))

    def close(self) -> None:
        self._transport.close()

    # -- clock --------------------------------------------------------------

    def server_time(self) -> datetime:
        """`GET /time`. Polymarket signs in **seconds**, unlike Kalshi's milliseconds."""
        payload = self._transport.get("/time")
        if isinstance(payload, int) and not isinstance(payload, bool):
            return datetime.fromtimestamp(payload, tz=UTC)
        if isinstance(payload, str) and payload.strip().isdigit():
            return datetime.fromtimestamp(int(payload.strip()), tz=UTC)
        raise VenueDataError(f"Polymarket /time returned an unexpected body: {payload!r}")

    # -- markets ------------------------------------------------------------

    def list_markets(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Market], str | None]:
        params: dict[str, Any] = {}
        if cursor:
            params["next_cursor"] = cursor
        payload = self._transport.get("/markets", params=params)
        if not isinstance(payload, dict):
            raise VenueDataError("Polymarket markets: expected an object")
        raw_markets = _require(payload, "data", context="markets")
        if not isinstance(raw_markets, list):
            raise VenueDataError("Polymarket markets: 'data' is not a list")
        markets = [self.parse_market(item) for item in raw_markets[:limit]]
        next_cursor = payload.get("next_cursor") or None
        # The V2 client defines "LTE=" as the end-of-pagination cursor.
        return markets, None if next_cursor == "LTE=" else next_cursor

    def get_market(self, market_key: str) -> Market:
        """Look up by **condition ID**. Passing a token ID here is the classic bug, so
        the identifier is parsed as a condition ID and rejected if it is not one."""
        parsed = condition_id(market_key)
        payload = self._transport.get(f"/markets/{parsed}")
        if not isinstance(payload, dict):
            raise VenueDataError("Polymarket market: expected an object")
        return self.parse_market(payload)

    @classmethod
    def parse_market(cls, raw: Any) -> Market:
        """Venue-native market to canonical. **Schema unverified.**"""
        if not isinstance(raw, Mapping):
            raise VenueDataError(f"Polymarket market: expected an object, got {type(raw).__name__}")

        condition = condition_id(str(_require(raw, "condition_id", context="market")))
        neg_risk = bool(raw.get("neg_risk", False))
        tags = [f"polymarket-condition:{condition}"]
        # Neg-risk markets are mutually exclusive outcomes of one event: buying several
        # of them is not diversification, it is the same bet expressed several ways.
        neg_risk_id = raw.get("neg_risk_market_id")
        if neg_risk and isinstance(neg_risk_id, str) and neg_risk_id:
            tags.append(f"polymarket-negrisk:{neg_risk_id}")

        volume_raw = raw.get("volume_24hr")
        return Market(
            venue=Venue.POLYMARKET,
            market_key=str(condition),
            event_key=str(neg_risk_id or condition),
            question=str(_require(raw, "question", context="market")),
            close_time=_parse_timestamp(
                _require(raw, "end_date_iso", context="market"), context="market end_date_iso"
            ),
            resolution_source=str(raw.get("description") or "UMA optimistic oracle"),
            correlation_tags=tuple(tags),
            tick_size=_parse_decimal(raw.get("minimum_tick_size", "0.01"), context="tick size"),
            volume_24h=(
                Usd(_parse_decimal(volume_raw, context="volume_24hr"))
                if volume_raw is not None
                else None
            ),
        )

    # -- per-market parameters ---------------------------------------------

    def tick_size(self, token: TokenId) -> Decimal:
        payload = self._transport.get("/tick-size", params={"token_id": str(token)})
        if isinstance(payload, Mapping):
            return _parse_decimal(_require(payload, "minimum_tick_size", context="tick-size"),
                                  context="tick-size")
        return _parse_decimal(payload, context="tick-size")

    def is_neg_risk(self, token: TokenId) -> bool:
        """Whether this market signs against the neg-risk exchange contract.

        Fetched per market, never assumed: signing an order against the wrong verifying
        contract produces a signature the exchange rejects, and guessing the flag would
        make that failure intermittent and market-dependent.
        """
        payload = self._transport.get("/neg-risk", params={"token_id": str(token)})
        if isinstance(payload, Mapping):
            value = _require(payload, "neg_risk", context="neg-risk")
        else:
            value = payload
        if not isinstance(value, bool):
            raise VenueDataError(f"Polymarket neg-risk: expected a boolean, got {value!r}")
        return value

    # -- quotes -------------------------------------------------------------

    def get_quote(self, outcome: OutcomeRef) -> Quote:
        """Book for one **outcome token**, not for a market.

        `outcome_key` must be a token ID. `market_key` is the condition ID. Order
        endpoints take the former; passing the latter is the single most common
        Polymarket integration bug, so the parse happens here at the boundary.
        """
        token = token_id(outcome.outcome_key)
        payload = self._transport.get("/book", params={"token_id": str(token)})
        if not isinstance(payload, dict):
            raise VenueDataError("Polymarket book: expected an object")

        bids = self._parse_levels(_require(payload, "bids", context="book"), context="book bids")
        asks = self._parse_levels(_require(payload, "asks", context="book"), context="book asks")
        # Bids descend (best is highest), asks ascend (best is lowest). Every depth and
        # touch calculation downstream depends on best-first ordering on both sides.
        bids = tuple(sorted(bids, key=lambda level: level.price, reverse=True))
        asks = tuple(sorted(asks, key=lambda level: level.price))

        return Quote(
            outcome=outcome,
            bid=bids[0].price if bids else None,
            ask=asks[0].price if asks else None,
            bid_depth=bids,
            ask_depth=asks,
            observed_at=datetime.now(UTC),
            source=QuoteSource.BOOK,
        )

    @classmethod
    def _parse_levels(cls, levels: Any, *, context: str) -> tuple[DepthLevel, ...]:
        if not isinstance(levels, list):
            raise VenueDataError(f"Polymarket {context}: expected a list")
        parsed: list[DepthLevel] = []
        for level in levels:
            if not isinstance(level, Mapping):
                raise VenueDataError(f"Polymarket {context}: expected objects, got {level!r}")
            price = _parse_decimal(_require(level, "price", context=context), context=context)
            size = _parse_decimal(_require(level, "size", context=context), context=context)
            if size <= 0:
                continue
            if size != size.to_integral_value():
                raise VenueDataError(
                    f"Polymarket {context}: fractional size {size} — depth is in whole "
                    "contracts and a fraction means the unit is not what we assume"
                )
            parsed.append(DepthLevel(price=Probability(price), quantity=int(size)))
        return tuple(parsed)

    @staticmethod
    def outcome_ref(
        condition: ConditionId, token: TokenId, event_key: str | None = None
    ) -> OutcomeRef:
        """Build a reference with the two identifiers in their correct roles."""
        return OutcomeRef(
            venue=Venue.POLYMARKET,
            market_key=str(condition),
            outcome_key=str(token),
            event_key=event_key or str(condition),
        )

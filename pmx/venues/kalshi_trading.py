"""Kalshi trading client — the authenticated half.

**Every request and response shape in this module is unverified.** Kalshi's own published
code covers authentication and the market-data endpoints; it does not include order
placement, and the documentation is unreachable from this environment
(docs/api-notes.md §0). So the order payload below is inference, and it is the one place
in the system where a wrong guess produces a malformed *write* rather than a bad read.

Three things contain that risk:

* Every assumption lives in `_ORDER_REQUEST_SHAPE` and the parse helpers below, in one
  place, so first contact with the demo environment corrects one file.
* `SCHEMA_VERIFIED = False` gates it: `require_verified_schema()` raises unless the
  operator has confirmed the shape against a demo response, and the router's callers are
  expected to run that check at startup.
* Parsing is strict and never defaults. An unrecognised field is an error, not a zero.

Order of operations for verifying this module (Phase 2, demo environment):
    1. Place one order for one contract on the demo environment.
    2. Compare the request we send and the response we get to what is coded here.
    3. Correct this file, flip `SCHEMA_VERIFIED`, and record the real shapes in
       docs/api-notes.md with the date.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pmx.core.clock import Clock
from pmx.core.models import OrderState, ProposedOrder, Side, Venue
from pmx.core.money import Usd, cents_to_probability, probability_to_cents
from pmx.venues.base import TradingVenue, VenueDataError, VenueError
from pmx.venues.kalshi import API_PREFIX, KalshiAuth, KalshiMarketData
from pmx.venues.transport import HttpTransport

__all__ = ["SCHEMA_VERIFIED", "KalshiTradingVenue", "UnverifiedOrderSchema"]

SCHEMA_VERIFIED: Final = False

PORTFOLIO = f"{API_PREFIX}/portfolio"

#: The assumed order payload. Documented as data so the diff that corrects it is small
#: and obvious, rather than scattered through a function body.
_ORDER_REQUEST_SHAPE: Final = {
    "ticker": "<market ticker>",
    "client_order_id": "<our idempotency key>",
    "side": "yes | no",
    "action": "buy | sell",
    "count": "<integer contracts>",
    "type": "limit",
    "yes_price": "<integer cents, when side is yes>",
    "no_price": "<integer cents, when side is no>",
}

#: Kalshi's own status strings, mapped to canonical states. Anything not listed leaves
#: the order unresolved rather than being guessed into a terminal state.
_STATUS_MAP: Final = {
    "resting": OrderState.SUBMITTED,
    "open": OrderState.SUBMITTED,
    "pending": OrderState.SUBMITTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "executed": OrderState.FILLED,
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "expired": OrderState.REJECTED,
}


class UnverifiedOrderSchema(RuntimeError):
    """The order schema has not been checked against a real venue response."""


def require_verified_schema() -> None:
    """Call at startup before enabling live trading.

    Deliberately not enforced inside `place_order`: making it a startup check means the
    system refuses to *start* in a live configuration it cannot honour, rather than
    discovering the problem with an order half-formed.
    """
    if not SCHEMA_VERIFIED:
        raise UnverifiedOrderSchema(
            "pmx/venues/kalshi_trading.py has not been verified against a real Kalshi "
            "response. Place one contract on the demo environment, compare the payloads, "
            "correct this module, then set SCHEMA_VERIFIED = True."
        )


def _require(payload: Mapping[str, Any], key: str, *, context: str) -> Any:
    if key not in payload:
        raise VenueDataError(
            f"Kalshi {context}: expected field {key!r}, got keys {sorted(payload)}. "
            "This schema is unverified — see pmx/venues/kalshi_trading.py."
        )
    return payload[key]


class KalshiTradingVenue(KalshiMarketData, TradingVenue):
    """Authenticated Kalshi client. Order methods are router-only by construction."""

    venue = Venue.KALSHI

    def __init__(
        self, transport: HttpTransport, auth: KalshiAuth, clock: Clock | None = None
    ) -> None:
        super().__init__(transport, auth)
        # Distinct from the parent's optional `_auth`: market data may run
        # unauthenticated, trading may not, and the type should say so.
        self._signer = auth
        self._clock = clock or Clock()

    # -- authenticated request plumbing ------------------------------------

    def _headers(self, method: str, path: str) -> dict[str, str]:
        """Sign with skew-corrected time.

        Kalshi rejects a signature whose timestamp has drifted, and the failure is a 401
        that looks exactly like a bad key. Using the clock's measured skew rather than
        the host clock removes the most common cause.
        """
        return self._signer.headers(self._clock.epoch_millis_for("kalshi"), method, path)

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._transport.get(path, params=params, headers=self._headers("GET", path))

    # -- orders -------------------------------------------------------------

    def place_order(self, order: ProposedOrder) -> str:
        """Submit one limit order. Called only by `pmx/execution/router.py`.

        The idempotency key travels as `client_order_id`. If the response is lost, that
        key is how recovery finds out whether this order exists — so it is required, not
        an optimisation.
        """
        ticker, side_label = self._split_outcome_key(order.outcome.outcome_key)

        # A price that is not on Kalshi's 1-cent tick raises rather than rounding: a
        # rounded price is a price the risk engine never approved.
        price_cents = probability_to_cents(order.limit_price)

        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": order.idempotency_key,
            "side": side_label.lower(),
            "action": "buy" if order.side is Side.BUY else "sell",
            "count": order.quantity,
            "type": "limit",
            f"{side_label.lower()}_price": price_cents,
        }

        path = f"{PORTFOLIO}/orders"
        payload = self._transport.post(path, json=body, headers=self._headers("POST", path))
        if not isinstance(payload, Mapping):
            raise VenueDataError("Kalshi place order: expected an object")
        placed = _require(payload, "order", context="place order")
        if not isinstance(placed, Mapping):
            raise VenueDataError("Kalshi place order: 'order' is not an object")
        return str(_require(placed, "order_id", context="place order"))

    def cancel_order(self, order_id: str) -> None:
        """Cancel by venue order id. Router only."""
        path = f"{PORTFOLIO}/orders/{order_id}"
        self._transport.delete(path, headers=self._headers("DELETE", path))

    def open_orders(self) -> list[Any]:
        """Live orders per the venue. The reconciler's and recovery's source of truth."""
        payload = self._get(f"{PORTFOLIO}/orders", {"status": "resting"})
        if not isinstance(payload, Mapping):
            raise VenueDataError("Kalshi open orders: expected an object")
        raw = _require(payload, "orders", context="open orders")
        if not isinstance(raw, list):
            raise VenueDataError("Kalshi open orders: 'orders' is not a list")
        return [self._normalize_order(entry) for entry in raw]

    def get_order(self, idempotency_key: str) -> dict[str, Any] | None:
        """Find one order by *our* key, including closed ones.

        Matching on `client_order_id` rather than on ticker, price and size is the whole
        point: attribute matching would happily pair our order with a second copy of it,
        which is precisely the ambiguity recovery exists to resolve.
        """
        payload = self._get(f"{PORTFOLIO}/orders", {"client_order_id": idempotency_key})
        if not isinstance(payload, Mapping):
            raise VenueDataError("Kalshi get order: expected an object")
        raw = payload.get("orders")
        if not isinstance(raw, list):
            raise VenueDataError("Kalshi get order: 'orders' is not a list")
        for entry in raw:
            normalized = self._normalize_order(entry)
            if normalized.get("idempotency_key") == idempotency_key:
                return normalized
        return None

    @staticmethod
    def _normalize_order(entry: Any) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            raise VenueDataError(f"Kalshi order: expected an object, got {type(entry).__name__}")
        raw_status = str(_require(entry, "status", context="order")).lower()
        return {
            "idempotency_key": entry.get("client_order_id"),
            "venue_order_id": str(_require(entry, "order_id", context="order")),
            "status": raw_status,
            # None, not a guess: an unrecognised status must leave recovery unresolved
            # so it halts, rather than being coerced into a terminal state.
            "canonical_state": _STATUS_MAP.get(raw_status),
            "outcome_key": KalshiTradingVenue._outcome_key_from(entry),
            "quantity": entry.get("remaining_count", entry.get("count")),
        }

    @staticmethod
    def _outcome_key_from(entry: Mapping[str, Any]) -> str:
        ticker = str(_require(entry, "ticker", context="order"))
        side = str(_require(entry, "side", context="order")).upper()
        if side not in {"YES", "NO"}:
            raise VenueDataError(f"Kalshi order: side must be yes or no, got {side!r}")
        return f"{ticker}:{side}"

    # -- account ------------------------------------------------------------

    def positions(self) -> list[Any]:
        """Positions per the venue. Where we disagree, the venue is right."""
        payload = self._get(f"{PORTFOLIO}/positions")
        if not isinstance(payload, Mapping):
            raise VenueDataError("Kalshi positions: expected an object")
        raw = _require(payload, "market_positions", context="positions")
        if not isinstance(raw, list):
            raise VenueDataError("Kalshi positions: 'market_positions' is not a list")

        normalized: list[Any] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise VenueDataError("Kalshi positions: expected objects")
            ticker = str(_require(entry, "ticker", context="positions"))
            quantity = _require(entry, "position", context="positions")
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                raise VenueDataError(
                    f"Kalshi positions: expected integer position, got {quantity!r}"
                )
            # Kalshi reports a signed position on the YES side: positive is long YES,
            # negative is long NO. Splitting it into our per-side outcome keys keeps the
            # two from being netted into one exposure by accident.
            side = "YES" if quantity >= 0 else "NO"
            normalized.append({"outcome_key": f"{ticker}:{side}", "quantity": abs(quantity)})
        return normalized

    def balance(self) -> Usd:
        """Settled cash, converted from Kalshi's integer cents at the boundary."""
        payload = self._get(f"{PORTFOLIO}/balance")
        if not isinstance(payload, Mapping):
            raise VenueDataError("Kalshi balance: expected an object")
        cents = _require(payload, "balance", context="balance")
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise VenueDataError(f"Kalshi balance: expected integer cents, got {cents!r}")
        return Usd(cents) / 100

    def fee_charged(self, entry: Mapping[str, Any]) -> Usd:
        """Extract the fee the venue actually charged on a fill.

        This is the input to the fee-model divergence halt, which is the only continuous
        check we have on a fee schedule that could not be read from primary documentation.
        """
        cents = entry.get("fee_paid_cents", entry.get("fees"))
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise VenueDataError(
                f"Kalshi fill: expected integer fee cents, got {cents!r}. Without a "
                "charged fee the model cannot be validated against reality."
            )
        return Usd(cents) / 100

    @staticmethod
    def price_from_cents(cents: int) -> Any:
        """Exposed for tests and for the reparse path; conversion lives at the boundary."""
        return cents_to_probability(cents)


def kalshi_trading_error(message: str) -> VenueError:
    return VenueError(message)

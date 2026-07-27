"""Kalshi trading client.

Every shape here is unverified inference (see the module docstring), so these tests do
two jobs: pin the current assumption so a correction is a visible diff, and prove the
behaviours that must hold whatever the shapes turn out to be — the idempotency key
travels as `client_order_id`, prices convert at the boundary and never round, an
unrecognised status resolves to nothing rather than to a guess, and requests are signed.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from pmx.core.models import OrderState, ProposedOrder, Side, Venue
from pmx.core.money import Probability, Usd
from pmx.venues.base import VenueDataError
from pmx.venues.kalshi import KalshiAuth, KalshiMarketData
from pmx.venues.kalshi_trading import (
    SCHEMA_VERIFIED,
    KalshiTradingVenue,
    UnverifiedOrderSchema,
    require_verified_schema,
)
from pmx.venues.transport import HttpTransport, TokenBucket
from tests.conftest import NOW


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def auth(rsa_key) -> KalshiAuth:
    return KalshiAuth("key-id", rsa_key)


def venue_for(handler, auth) -> KalshiTradingVenue:
    client = httpx.Client(base_url="https://kalshi.test", transport=httpx.MockTransport(handler))
    transport = HttpTransport(
        "https://kalshi.test",
        read_bucket=TokenBucket(capacity=100.0, refill_per_second=1000.0),
        client=client,
        sleep=lambda _s: None,
    )
    return KalshiTradingVenue(transport, auth)


def order(**overrides) -> ProposedOrder:
    outcome = KalshiMarketData.outcome_ref("FED-26SEP-C025", "FED-26SEP", "YES")
    fields = {
        "idempotency_key": "abc123deadbeef",
        "signal_id": "sig-1",
        "strategy": "manual",
        "outcome": outcome,
        "side": Side.BUY,
        "limit_price": Probability("0.40"),
        "quantity": 100,
        "max_cost": Usd("42"),
        "estimated_fee": Usd("2"),
        "kelly_raw_quantity": 500,
        "kelly_capped_quantity": 100,
        "created_at": NOW,
    }
    fields.update(overrides)
    return ProposedOrder(**fields)


class TestSchemaGate:
    def test_schema_is_not_yet_verified(self) -> None:
        assert SCHEMA_VERIFIED is False

    def test_startup_check_refuses_an_unverified_schema(self) -> None:
        """A startup failure, not a mid-order one: the system should refuse to come up in
        a live configuration it cannot honour."""
        with pytest.raises(UnverifiedOrderSchema, match="demo environment"):
            require_verified_schema()


class TestPlaceOrder:
    def test_request_carries_the_idempotency_key_as_client_order_id(self, auth) -> None:
        """This is how recovery finds the order when the response is lost. Without it,
        an order that landed is indistinguishable from one that did not."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"order": {"order_id": "venue-1"}})

        venue = venue_for(handler, auth)
        assert venue.place_order(order()) == "venue-1"
        assert captured["client_order_id"] == "abc123deadbeef"

    def test_price_converts_to_integer_cents_at_the_boundary(self, auth) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"order": {"order_id": "v"}})

        venue_for(handler, auth).place_order(order())
        assert captured["yes_price"] == 40
        assert captured["count"] == 100
        assert captured["type"] == "limit"
        assert captured["action"] == "buy"
        assert captured["side"] == "yes"

    def test_off_tick_price_raises_rather_than_rounding(self, auth) -> None:
        """A rounded price is a price the risk engine never approved."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"order": {"order_id": "v"}})

        venue = venue_for(handler, auth)
        with pytest.raises(ValueError, match="1-cent tick"):
            venue.place_order(order(limit_price=Probability("0.405")))

    def test_no_side_uses_the_no_price_field(self, auth) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"order": {"order_id": "v"}})

        outcome = KalshiMarketData.outcome_ref("FED-26SEP-C025", "FED-26SEP", "NO")
        venue_for(handler, auth).place_order(order(outcome=outcome))
        assert captured["side"] == "no"
        assert captured["no_price"] == 40
        assert "yes_price" not in captured

    def test_sell_maps_to_the_sell_action(self, auth) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"order": {"order_id": "v"}})

        # A short at 0.40 risks 0.60/contract, so max_cost must cover $60 — the model's
        # own validator rejects anything less, which is the Phase 0 short-risk fix
        # showing up here as a test that cannot be written wrong.
        venue_for(handler, auth).place_order(order(side=Side.SELL, max_cost=Usd("62")))
        assert captured["action"] == "sell"

    def test_request_is_signed(self, auth) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, json={"order": {"order_id": "v"}})

        venue_for(handler, auth).place_order(order())
        assert "kalshi-access-signature" in seen
        assert "kalshi-access-timestamp" in seen
        assert seen["kalshi-access-key"] == "key-id"

    def test_missing_order_id_raises(self, auth) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"order": {}})

        with pytest.raises(VenueDataError, match="order_id"):
            venue_for(handler, auth).place_order(order())


class TestOrderLookup:
    ORDERS: ClassVar[dict] = {
        "orders": [
            {
                "order_id": "venue-1",
                "client_order_id": "abc123deadbeef",
                "status": "resting",
                "ticker": "FED-26SEP-C025",
                "side": "yes",
                "remaining_count": 100,
            }
        ]
    }

    def test_open_orders_are_normalized(self, auth) -> None:
        venue = venue_for(lambda _r: httpx.Response(200, json=self.ORDERS), auth)
        orders = venue.open_orders()
        assert orders[0]["idempotency_key"] == "abc123deadbeef"
        assert orders[0]["outcome_key"] == "FED-26SEP-C025:YES"
        assert orders[0]["canonical_state"] is OrderState.SUBMITTED

    def test_get_order_matches_on_our_key_not_on_attributes(self, auth) -> None:
        """Attribute matching would pair our order with a second copy of it."""
        venue = venue_for(lambda _r: httpx.Response(200, json=self.ORDERS), auth)
        assert venue.get_order("abc123deadbeef") is not None
        assert venue.get_order("some-other-key") is None

    def test_unknown_status_resolves_to_nothing(self, auth) -> None:
        """An unrecognised status must leave recovery unresolved so it halts, rather than
        being coerced into a terminal state we invented."""
        payload = {
            "orders": [
                {
                    "order_id": "v",
                    "client_order_id": "k",
                    "status": "some_new_status",
                    "ticker": "T",
                    "side": "yes",
                }
            ]
        }
        venue = venue_for(lambda _r: httpx.Response(200, json=payload), auth)
        assert venue.open_orders()[0]["canonical_state"] is None

    def test_filled_and_canceled_map_to_terminal_states(self, auth) -> None:
        for status, expected in [
            ("executed", OrderState.FILLED),
            ("canceled", OrderState.CANCELED),
            ("rejected", OrderState.REJECTED),
            ("partially_filled", OrderState.PARTIALLY_FILLED),
        ]:
            payload = {
                "orders": [
                    {
                        "order_id": "v",
                        "client_order_id": "k",
                        "status": status,
                        "ticker": "T",
                        "side": "no",
                    }
                ]
            }
            venue = venue_for(lambda _r, p=payload: httpx.Response(200, json=p), auth)
            assert venue.open_orders()[0]["canonical_state"] is expected

    def test_bad_side_raises(self, auth) -> None:
        payload = {
            "orders": [
                {"order_id": "v", "client_order_id": "k", "status": "resting",
                 "ticker": "T", "side": "maybe"}
            ]
        }
        venue = venue_for(lambda _r: httpx.Response(200, json=payload), auth)
        with pytest.raises(VenueDataError, match="yes or no"):
            venue.open_orders()


class TestAccount:
    def test_balance_converts_cents_to_dollars(self, auth) -> None:
        venue = venue_for(lambda _r: httpx.Response(200, json={"balance": 123456}), auth)
        assert venue.balance() == Usd("1234.56")

    def test_non_integer_balance_raises(self, auth) -> None:
        venue = venue_for(lambda _r: httpx.Response(200, json={"balance": "1234.56"}), auth)
        with pytest.raises(VenueDataError, match="integer cents"):
            venue.balance()

    def test_signed_position_splits_into_per_side_outcome_keys(self, auth) -> None:
        """A long YES and a long NO are different exposures; netting them into one key
        would let two offsetting positions look like a single small one."""
        payload = {
            "market_positions": [
                {"ticker": "FED-A", "position": 100},
                {"ticker": "FED-B", "position": -60},
            ]
        }
        venue = venue_for(lambda _r: httpx.Response(200, json=payload), auth)
        positions = venue.positions()
        assert positions[0] == {"outcome_key": "FED-A:YES", "quantity": 100}
        assert positions[1] == {"outcome_key": "FED-B:NO", "quantity": 60}

    def test_fee_charged_is_required_for_the_divergence_check(self, auth) -> None:
        """Without a charged fee there is nothing to validate the model against, and the
        fee model is the thing most likely to be wrong."""
        venue = venue_for(lambda _r: httpx.Response(200, json={}), auth)
        assert venue.fee_charged({"fee_paid_cents": 175}) == Usd("1.75")
        with pytest.raises(VenueDataError, match="cannot be validated"):
            venue.fee_charged({})


class TestInterfaceCompliance:
    def test_it_is_a_trading_venue(self, auth) -> None:
        from pmx.venues.base import TradingVenue

        venue = venue_for(lambda _r: httpx.Response(200, json={}), auth)
        assert isinstance(venue, TradingVenue)
        assert venue.venue is Venue.KALSHI

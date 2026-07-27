"""Polymarket CLOB client.

The condition-ID/token-ID tests are the point of this file. Both are long opaque
strings, both parse as JSON strings, and swapping them produces a well-formed request
against the wrong object — the failure mode docs/api-notes.md §2.4 calls the #1
integration bug. Response schemas here are unverified and parsed strictly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from pmx.core.ids import IdFormatError, condition_id, token_id
from pmx.core.models import OutcomeRef, QuoteSource, Venue
from pmx.core.money import Probability, Usd
from pmx.venues.base import VenueDataError
from pmx.venues.polymarket import PolymarketMarketData
from pmx.venues.transport import HttpTransport, TokenBucket

CONDITION = "0x" + "ab" * 32
TOKEN_YES = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
TOKEN_NO = "52114319501245915516055106046884209969926127482827954674443846427813813222426"

MARKET_FIXTURE = {
    "condition_id": CONDITION,
    "question": "Will the Fed cut rates in September 2026?",
    "end_date_iso": "2026-09-16T18:00:00Z",
    "description": "Resolves via UMA optimistic oracle.",
    "minimum_tick_size": "0.001",
    "volume_24hr": "125000.50",
    "neg_risk": False,
    "tokens": [
        {"token_id": TOKEN_YES, "outcome": "Yes"},
        {"token_id": TOKEN_NO, "outcome": "No"},
    ],
}

BOOK_FIXTURE = {
    "market": CONDITION,
    "asset_id": TOKEN_YES,
    "bids": [{"price": "0.38", "size": "500"}, {"price": "0.37", "size": "1200"}],
    "asks": [{"price": "0.40", "size": "800"}, {"price": "0.41", "size": "2000"}],
}


def transport_for(handler) -> HttpTransport:
    client = httpx.Client(base_url="https://clob.test", transport=httpx.MockTransport(handler))
    return HttpTransport(
        "https://clob.test",
        read_bucket=TokenBucket(capacity=100.0, refill_per_second=100.0),
        client=client,
        sleep=lambda _seconds: None,
    )


class TestIdentifierRoles:
    def test_market_lookup_takes_a_condition_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"/markets/{CONDITION}"
            return httpx.Response(200, json=MARKET_FIXTURE)

        venue = PolymarketMarketData(transport_for(handler))
        assert venue.get_market(CONDITION).market_key == CONDITION

    def test_market_lookup_rejects_a_token_id(self) -> None:
        """The bug in its natural habitat: a token ID reaching a market endpoint."""
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json={})))
        with pytest.raises(IdFormatError, match="0x"):
            venue.get_market(TOKEN_YES)

    def test_book_lookup_takes_a_token_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["token_id"] == TOKEN_YES
            return httpx.Response(200, json=BOOK_FIXTURE)

        venue = PolymarketMarketData(transport_for(handler))
        outcome = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        assert venue.get_quote(outcome).bid == Probability("0.38")

    def test_book_lookup_rejects_a_condition_id_in_the_outcome_slot(self) -> None:
        """A condition ID here would return the wrong book, or someone else's, and every
        price downstream would be for a different question."""
        venue = PolymarketMarketData(
            transport_for(lambda _r: httpx.Response(200, json=BOOK_FIXTURE))
        )
        wrong = OutcomeRef(
            venue=Venue.POLYMARKET,
            market_key=CONDITION,
            outcome_key=CONDITION,  # a condition id where a token id belongs
            event_key=CONDITION,
        )
        with pytest.raises(IdFormatError):
            venue.get_quote(wrong)

    def test_outcome_ref_keeps_the_two_ids_in_their_roles(self) -> None:
        ref = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        assert ref.market_key == CONDITION
        assert ref.outcome_key == TOKEN_YES

    def test_the_two_outcomes_of_one_market_share_a_market_key(self) -> None:
        yes = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        no = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_NO))
        assert yes.market_key == no.market_key
        assert yes.outcome_key != no.outcome_key


class TestMarketParsing:
    def test_parses_the_assumed_schema(self) -> None:
        market = PolymarketMarketData.parse_market(MARKET_FIXTURE)
        assert market.venue is Venue.POLYMARKET
        assert market.close_time == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
        assert market.tick_size == Decimal("0.001")
        assert market.volume_24h == Usd("125000.50")

    def test_neg_risk_group_becomes_a_correlation_tag(self) -> None:
        """Neg-risk outcomes are mutually exclusive answers to one question. Holding
        several is not diversification; it is the same bet several times."""
        market = PolymarketMarketData.parse_market(
            {**MARKET_FIXTURE, "neg_risk": True, "neg_risk_market_id": "0xfeed"}
        )
        assert "polymarket-negrisk:0xfeed" in market.correlation_tags
        assert market.event_key == "0xfeed"

    def test_missing_condition_id_raises(self) -> None:
        broken = {key: value for key, value in MARKET_FIXTURE.items() if key != "condition_id"}
        with pytest.raises(VenueDataError, match="condition_id"):
            PolymarketMarketData.parse_market(broken)

    def test_missing_end_date_raises(self) -> None:
        broken = {key: value for key, value in MARKET_FIXTURE.items() if key != "end_date_iso"}
        with pytest.raises(VenueDataError, match="end_date_iso"):
            PolymarketMarketData.parse_market(broken)

    def test_unknown_volume_stays_unknown(self) -> None:
        broken = {key: value for key, value in MARKET_FIXTURE.items() if key != "volume_24hr"}
        assert PolymarketMarketData.parse_market(broken).volume_24h is None


class TestNoFloatsFromTheWire:
    def test_a_json_float_price_is_rejected_not_converted(self) -> None:
        """By the time json.loads has produced a float the precision is already gone.
        Converting it would launder a lossy value into the money path."""
        book = {**BOOK_FIXTURE, "bids": [{"price": 0.38, "size": "500"}]}
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json=book)))
        outcome = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        with pytest.raises(VenueDataError, match="float"):
            venue.get_quote(outcome)

    def test_fractional_size_is_rejected(self) -> None:
        """Depth is in whole contracts. A fraction means the unit is not what we assume,
        and a wrong depth unit silently breaks the book-percentage limit."""
        book = {**BOOK_FIXTURE, "bids": [{"price": "0.38", "size": "1.5"}]}
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json=book)))
        outcome = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        with pytest.raises(VenueDataError, match="fractional size"):
            venue.get_quote(outcome)


class TestBookOrdering:
    def test_bids_descend_and_asks_ascend(self) -> None:
        scrambled = {
            **BOOK_FIXTURE,
            "bids": [{"price": "0.37", "size": "1200"}, {"price": "0.38", "size": "500"}],
            "asks": [{"price": "0.41", "size": "2000"}, {"price": "0.40", "size": "800"}],
        }
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json=scrambled)))
        outcome = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        quote = venue.get_quote(outcome)
        assert quote.bid == Probability("0.38")
        assert quote.ask == Probability("0.40")
        assert quote.source is QuoteSource.BOOK

    def test_empty_book_yields_no_touch(self) -> None:
        empty = {**BOOK_FIXTURE, "bids": [], "asks": []}
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json=empty)))
        outcome = PolymarketMarketData.outcome_ref(condition_id(CONDITION), token_id(TOKEN_YES))
        quote = venue.get_quote(outcome)
        assert quote.bid is None
        assert quote.ask is None


class TestPerMarketParameters:
    def test_neg_risk_is_fetched_not_assumed(self) -> None:
        """Signing against the wrong verifying contract yields a rejected signature that
        depends on which market you happened to pick."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"neg_risk": True})

        venue = PolymarketMarketData(transport_for(handler))
        assert venue.is_neg_risk(token_id(TOKEN_YES)) is True
        assert seen == ["/neg-risk"]

    def test_non_boolean_neg_risk_raises(self) -> None:
        venue = PolymarketMarketData(
            transport_for(lambda _r: httpx.Response(200, json={"neg_risk": "yes"}))
        )
        with pytest.raises(VenueDataError, match="boolean"):
            venue.is_neg_risk(token_id(TOKEN_YES))

    def test_tick_size_is_decimal(self) -> None:
        venue = PolymarketMarketData(
            transport_for(lambda _r: httpx.Response(200, json={"minimum_tick_size": "0.001"}))
        )
        assert venue.tick_size(token_id(TOKEN_YES)) == Decimal("0.001")


class TestServerTime:
    def test_time_is_seconds(self) -> None:
        """Polymarket signs in seconds; Kalshi in milliseconds. Confusing them is a 401."""
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json=1785240000)))
        assert venue.server_time() == datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def test_unexpected_body_raises(self) -> None:
        venue = PolymarketMarketData(transport_for(lambda _r: httpx.Response(200, json={"t": 1})))
        with pytest.raises(VenueDataError, match="/time"):
            venue.server_time()


class TestPagination:
    def test_end_cursor_terminates_pagination(self) -> None:
        """The V2 client defines LTE= as the end sentinel; treating it as a real cursor
        would loop forever."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [MARKET_FIXTURE], "next_cursor": "LTE="})

        venue = PolymarketMarketData(transport_for(handler))
        _markets, cursor = venue.list_markets()
        assert cursor is None

    def test_chain_id_must_be_known(self) -> None:
        with pytest.raises(ValueError, match="chain id"):
            PolymarketMarketData(transport_for(lambda _r: httpx.Response(200)), chain_id=1)

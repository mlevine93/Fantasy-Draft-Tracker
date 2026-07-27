"""Kalshi client.

The signer is tested against a real RSA key generated in the test: the signature is
verified with the public key, which proves the message construction and PSS parameters
rather than asserting a hardcoded blob. That matters because the two ways to get this
wrong — seconds instead of milliseconds, MAX_LENGTH instead of DIGEST_LENGTH — both
produce a well-formed signature that the venue rejects with a 401 that looks like a
credential problem.

Parsing is tested against fixtures whose shape is **unverified** (docs/api-notes.md §0).
Those tests pin our current assumption so that a schema correction is a visible diff,
and they prove the strict-parsing behaviour: a missing field raises rather than defaults.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pmx.core.models import QuoteSource, Venue
from pmx.core.money import Probability, Usd
from pmx.venues.base import VenueDataError
from pmx.venues.kalshi import API_PREFIX, KalshiAuth, KalshiMarketData
from pmx.venues.transport import HttpTransport, TokenBucket

MARKET_FIXTURE = {
    "ticker": "FED-26SEP-C025",
    "event_ticker": "FED-26SEP",
    "series_ticker": "FED",
    "title": "Will the Fed cut rates at the September 2026 meeting?",
    "close_time": "2026-09-16T18:00:00Z",
    "rules_primary": "Resolves per the FOMC statement.",
    "volume_24h": 12345,
    "yes_bid": 38,
    "yes_ask": 40,
}

ORDERBOOK_FIXTURE = {
    "orderbook": {
        "yes": [[38, 500], [37, 1200], [35, 3000]],
        "no": [[60, 400], [59, 900]],
    }
}


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def auth(rsa_key: rsa.RSAPrivateKey) -> KalshiAuth:
    return KalshiAuth("test-key-id", rsa_key)


def transport_for(handler) -> HttpTransport:
    client = httpx.Client(
        base_url="https://kalshi.test", transport=httpx.MockTransport(handler)
    )
    return HttpTransport(
        "https://kalshi.test",
        read_bucket=TokenBucket(capacity=100.0, refill_per_second=100.0),
        client=client,
        sleep=lambda _seconds: None,
    )


class TestSigner:
    def test_signature_verifies_against_the_public_key(self, auth, rsa_key) -> None:
        timestamp = 1_703_123_456_789
        message = auth.signing_message(timestamp, "GET", "/trade-api/v2/portfolio/balance")
        signature = base64.b64decode(auth.sign(timestamp, "GET", "/trade-api/v2/portfolio/balance"))

        # Raises InvalidSignature if the message or the PSS parameters are wrong.
        rsa_key.public_key().verify(
            signature,
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def test_message_is_timestamp_method_path(self, auth) -> None:
        assert (
            auth.signing_message(1_703_123_456_789, "get", "/trade-api/v2/portfolio/balance")
            == "1703123456789GET/trade-api/v2/portfolio/balance"
        )

    def test_query_string_is_not_signed(self, auth) -> None:
        with_query = auth.signing_message(1, "GET", "/trade-api/v2/portfolio/orders?limit=5")
        without = auth.signing_message(1, "GET", "/trade-api/v2/portfolio/orders")
        assert with_query == without

    def test_salt_length_max_would_not_verify_as_digest_length(self, rsa_key) -> None:
        """The specific mistake this pins: MAX_LENGTH produces a valid-looking signature
        that verification under DIGEST_LENGTH rejects."""
        message = b"1GET/x"
        wrong = rsa_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        with pytest.raises(InvalidSignature):
            rsa_key.public_key().verify(
                wrong,
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256(),
            )

    def test_headers_carry_the_documented_names(self, auth) -> None:
        headers = auth.headers(1_703_123_456_789, "GET", "/trade-api/v2/exchange/status")
        assert set(headers) == {
            "Content-Type",
            "KALSHI-ACCESS-KEY",
            "KALSHI-ACCESS-SIGNATURE",
            "KALSHI-ACCESS-TIMESTAMP",
        }
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"

    def test_timestamp_is_milliseconds_not_seconds(self, auth) -> None:
        """13 digits, not 10. A seconds timestamp is silently accepted by the signer and
        rejected by the venue."""
        assert len(auth.headers(1_703_123_456_789, "GET", "/x")["KALSHI-ACCESS-TIMESTAMP"]) == 13

    def test_empty_key_id_rejected(self, rsa_key) -> None:
        with pytest.raises(ValueError, match="key id"):
            KalshiAuth("", rsa_key)

    def test_missing_key_file_is_an_error_not_a_silent_skip(self) -> None:
        with pytest.raises(FileNotFoundError):
            KalshiAuth.from_key_file("id", "/nonexistent/kalshi.pem")


class TestMarketParsing:
    def test_parses_the_assumed_schema(self) -> None:
        market = KalshiMarketData.parse_market(MARKET_FIXTURE)
        assert market.venue is Venue.KALSHI
        assert market.market_key == "FED-26SEP-C025"
        assert market.event_key == "FED-26SEP"
        assert market.close_time == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
        assert market.volume_24h == Usd("12345")

    def test_event_and_series_both_become_correlation_tags(self) -> None:
        """Ten markets on one event are one bet; the tags are how the risk engine knows."""
        market = KalshiMarketData.parse_market(MARKET_FIXTURE)
        assert market.correlation_tags == ("kalshi-event:FED-26SEP", "kalshi-series:FED")

    def test_missing_event_ticker_raises_rather_than_defaulting(self) -> None:
        """A market with no event key would be treated as uncorrelated with the other
        markets on its own event — the exact failure the limit exists to prevent."""
        broken = {key: value for key, value in MARKET_FIXTURE.items() if key != "event_ticker"}
        with pytest.raises(VenueDataError, match="event_ticker"):
            KalshiMarketData.parse_market(broken)

    def test_missing_close_time_raises(self) -> None:
        broken = {key: value for key, value in MARKET_FIXTURE.items() if key != "close_time"}
        with pytest.raises(VenueDataError, match="close_time"):
            KalshiMarketData.parse_market(broken)

    def test_naive_close_time_raises(self) -> None:
        broken = {**MARKET_FIXTURE, "close_time": "2026-09-16T18:00:00"}
        with pytest.raises(VenueDataError, match="timezone"):
            KalshiMarketData.parse_market(broken)

    def test_unknown_volume_stays_unknown(self) -> None:
        """None must not become zero: the risk engine treats unknown volume as illiquid,
        and a fabricated zero would be indistinguishable from a real measurement."""
        broken = {key: value for key, value in MARKET_FIXTURE.items() if key != "volume_24h"}
        assert KalshiMarketData.parse_market(broken).volume_24h is None

    def test_non_object_raises(self) -> None:
        with pytest.raises(VenueDataError, match="expected an object"):
            KalshiMarketData.parse_market(["not", "a", "market"])


class TestQuotes:
    def test_orderbook_becomes_a_canonical_quote(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == f"{API_PREFIX}/markets/FED-26SEP-C025/orderbook"
            return httpx.Response(200, json=ORDERBOOK_FIXTURE)

        venue = KalshiMarketData(transport_for(handler))
        outcome = KalshiMarketData.outcome_ref("FED-26SEP-C025", "FED-26SEP", "YES")
        quote = venue.get_quote(outcome)

        assert quote.source is QuoteSource.BOOK
        # Cents to probability at the venue boundary, and nowhere else.
        assert quote.bid == Probability("0.38")
        assert [level.quantity for level in quote.bid_depth] == [500, 1200, 3000]

    def test_depth_is_best_price_first(self) -> None:
        """An inverted book reports the worst price as the touch and passes a slippage
        check it should fail."""
        shuffled = {"orderbook": {"yes": [[35, 3000], [38, 500], [37, 1200]]}}

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=shuffled)

        venue = KalshiMarketData(transport_for(handler))
        quote = venue.get_quote(KalshiMarketData.outcome_ref("T", "E", "YES"))
        prices = [level.price for level in quote.bid_depth]
        assert prices == sorted(prices, reverse=True)
        assert quote.bid == Probability("0.38")

    def test_zero_size_levels_are_dropped(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"orderbook": {"yes": [[38, 0], [37, 100]]}})

        venue = KalshiMarketData(transport_for(handler))
        quote = venue.get_quote(KalshiMarketData.outcome_ref("T", "E", "YES"))
        assert [level.quantity for level in quote.bid_depth] == [100]

    def test_malformed_level_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"orderbook": {"yes": [[38, 100, 7]]}})

        venue = KalshiMarketData(transport_for(handler))
        with pytest.raises(VenueDataError, match=r"\[price, size\]"):
            venue.get_quote(KalshiMarketData.outcome_ref("T", "E", "YES"))

    def test_outcome_key_must_name_a_side(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=ORDERBOOK_FIXTURE)

        venue = KalshiMarketData(transport_for(handler))
        from pmx.core.models import OutcomeRef

        bad = OutcomeRef(venue=Venue.KALSHI, market_key="T", outcome_key="T", event_key="E")
        with pytest.raises(VenueDataError, match="YES.*NO"):
            venue.get_quote(bad)

    def test_yes_and_no_are_distinct_outcome_keys(self) -> None:
        """They offset, so summing them as one exposure would understate nothing and
        overstate everything — they must stay separate."""
        yes = KalshiMarketData.outcome_ref("T", "E", "YES")
        no = KalshiMarketData.outcome_ref("T", "E", "NO")
        assert yes.outcome_key != no.outcome_key
        assert yes.market_key == no.market_key
        assert yes.event_key == no.event_key


class TestServerTime:
    def test_clock_comes_from_the_http_date_header(self) -> None:
        """RFC 9110 defines this header, so it is one part of the integration that needs
        no schema guessing."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"exchange_active": True},
                headers={"Date": "Sat, 26 Jul 2026 12:00:00 GMT"},
            )

        venue = KalshiMarketData(transport_for(handler))
        assert venue.server_time() == datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    def test_missing_date_header_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={}, headers={"x-no-date": "1"})

        venue = KalshiMarketData(transport_for(handler))
        # httpx supplies no Date by default in MockTransport responses.
        with pytest.raises(VenueDataError, match="Date header"):
            venue.server_time()


class TestListMarkets:
    def test_pagination_cursor_is_returned(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": [MARKET_FIXTURE], "cursor": "abc"})

        venue = KalshiMarketData(transport_for(handler))
        markets, cursor = venue.list_markets()
        assert len(markets) == 1
        assert cursor == "abc"

    def test_empty_cursor_means_end_of_pages(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": [], "cursor": ""})

        venue = KalshiMarketData(transport_for(handler))
        _markets, cursor = venue.list_markets()
        assert cursor is None

    def test_missing_markets_key_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        venue = KalshiMarketData(transport_for(handler))
        with pytest.raises(VenueDataError, match="markets"):
            venue.list_markets()

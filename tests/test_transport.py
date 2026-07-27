"""HTTP transport.

The central assertion: **writes are never retried.** A POST that times out may have
executed, and repeating it turns one intended order into two real positions. Everything
else here is rate limiting and error classification.
"""

from __future__ import annotations

import httpx
import pytest

from pmx.venues.base import (
    VenueAuthError,
    VenueDataError,
    VenueError,
    VenueRateLimited,
    VenueUnavailable,
)
from pmx.venues.transport import HttpTransport, TokenBucket, redact_headers


def transport(handler, **kwargs) -> HttpTransport:
    client = httpx.Client(base_url="https://venue.test", transport=httpx.MockTransport(handler))
    kwargs.setdefault("read_bucket", TokenBucket(capacity=100.0, refill_per_second=1000.0))
    kwargs.setdefault("sleep", lambda _seconds: None)
    return HttpTransport("https://venue.test", client=client, **kwargs)


class TestRetryPolicy:
    def test_reads_retry_on_transport_error(self) -> None:
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.ConnectError("boom")
            return httpx.Response(200, json={"ok": True})

        assert transport(handler).get("/x") == {"ok": True}
        assert len(attempts) == 3

    def test_reads_retry_on_5xx(self) -> None:
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503 if len(attempts) < 2 else 200, json={"ok": True})

        assert transport(handler).get("/x") == {"ok": True}
        assert len(attempts) == 2

    def test_reads_give_up_eventually(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with pytest.raises(VenueUnavailable, match="attempts"):
            transport(handler, max_read_retries=2).get("/x")

    def test_writes_are_never_retried(self) -> None:
        """The whole point. One POST attempt, then the caller must go and look."""
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ReadTimeout("timeout")

        with pytest.raises(VenueUnavailable, match="may or may not have been executed"):
            transport(handler).post("/order", json={"a": 1})
        assert len(attempts) == 1, "a retried write can double a position"

    def test_writes_do_not_retry_on_5xx_either(self) -> None:
        """A 500 from an order endpoint does not mean the order was not accepted."""
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(500, text="server error")

        with pytest.raises(VenueError):
            transport(handler).post("/order", json={"a": 1})
        assert len(attempts) == 1

    def test_cancels_are_not_retried(self) -> None:
        attempts: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            raise httpx.ReadTimeout("timeout")

        with pytest.raises(VenueUnavailable, match="outcome unknown"):
            transport(handler).delete("/order")
        assert len(attempts) == 1


class TestErrorClassification:
    def test_429_is_a_circuit_event(self) -> None:
        """Not a hint to sleep: it means our budget accounting is wrong."""
        with pytest.raises(VenueRateLimited):
            transport(lambda _r: httpx.Response(429)).get("/x")

    def test_401_points_at_the_clock_first(self) -> None:
        with pytest.raises(VenueAuthError, match="clock skew"):
            transport(lambda _r: httpx.Response(401)).get("/x")

    def test_403_is_also_auth(self) -> None:
        with pytest.raises(VenueAuthError):
            transport(lambda _r: httpx.Response(403)).get("/x")

    def test_4xx_carries_the_body(self) -> None:
        with pytest.raises(VenueError, match="bad ticker"):
            transport(lambda _r: httpx.Response(400, text="bad ticker")).get("/x")

    def test_non_json_body_raises_data_error(self) -> None:
        with pytest.raises(VenueDataError, match="non-JSON"):
            transport(lambda _r: httpx.Response(200, text="<html>maintenance</html>")).get("/x")

    def test_empty_body_is_none_not_an_error(self) -> None:
        assert transport(lambda _r: httpx.Response(204)).get("/x") is None


class TestRateLimiting:
    def test_bucket_allows_a_burst_then_throttles(self) -> None:
        waits: list[float] = []
        bucket = TokenBucket(capacity=3.0, refill_per_second=1.0)
        for _ in range(3):
            waits.append(bucket.take(1.0, sleep=lambda _s: None))
        throttled = bucket.take(1.0, sleep=lambda _s: None)
        assert waits == [0.0, 0.0, 0.0]
        assert throttled > 0

    def test_a_request_larger_than_the_bucket_is_a_config_error(self) -> None:
        """Better to fail at startup than to block forever at 3am."""
        bucket = TokenBucket(capacity=5.0, refill_per_second=1.0)
        with pytest.raises(ValueError, match="never be sent"):
            bucket.take(10.0, sleep=lambda _s: None)

    def test_invalid_bucket_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(capacity=0.0, refill_per_second=1.0)

    def test_zero_cost_rejected(self) -> None:
        bucket = TokenBucket(capacity=5.0, refill_per_second=1.0)
        with pytest.raises(ValueError, match="positive"):
            bucket.take(0.0)

    def test_reads_and_writes_can_use_separate_budgets(self) -> None:
        read = TokenBucket(capacity=10.0, refill_per_second=10.0)
        write = TokenBucket(capacity=1.0, refill_per_second=1.0)
        client = transport(
            lambda _r: httpx.Response(200, json={}), read_bucket=read, write_bucket=write
        )
        assert client.read_bucket is read
        assert client.write_bucket is write


class TestRedaction:
    def test_credential_headers_are_redacted(self) -> None:
        cleaned = redact_headers(
            {
                "KALSHI-ACCESS-KEY": "real-key-id",
                "KALSHI-ACCESS-SIGNATURE": "c2lnbmF0dXJl",
                "POLY_PASSPHRASE": "secret",
                "Content-Type": "application/json",
            }
        )
        assert cleaned["KALSHI-ACCESS-KEY"] == "<redacted>"
        assert cleaned["KALSHI-ACCESS-SIGNATURE"] == "<redacted>"
        assert cleaned["POLY_PASSPHRASE"] == "<redacted>"
        assert cleaned["Content-Type"] == "application/json"

    def test_auth_error_message_contains_no_credentials(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="invalid signature for key abc123")

        with pytest.raises(VenueAuthError) as excinfo:
            transport(handler).get("/x", headers={"KALSHI-ACCESS-KEY": "abc123"})
        assert "abc123" not in str(excinfo.value)

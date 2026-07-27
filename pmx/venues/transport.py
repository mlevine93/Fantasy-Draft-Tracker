"""HTTP transport: rate limiting, retries, and the rule about which requests may repeat.

Two things here are load-bearing.

**Only idempotent reads are retried.** A GET that times out can be repeated freely. A
POST that times out may have been executed, and repeating it is how one intended order
becomes two real ones. §1.8 says never retry a write in an ambiguous state without first
querying that state — so this layer simply refuses to retry non-GET requests at all, and
raises `VenueUnavailable` for the caller to resolve against the venue's own records.

**Rate limits are a circuit event, not a hint.** A 429 means our accounting of the
venue's budget is wrong. We are not latency-sensitive (§12), so being slow costs nothing
and being banned costs everything: the limiter is configured pessimistically and a 429
raises rather than sleeping and continuing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from pmx.venues.base import (
    VenueAuthError,
    VenueDataError,
    VenueError,
    VenueRateLimited,
    VenueUnavailable,
)

__all__ = ["HttpTransport", "TokenBucket"]

#: Headers whose values must never reach a log or an exception message.
_SENSITIVE_HEADERS: Final = frozenset(
    {
        "kalshi-access-key",
        "kalshi-access-signature",
        "poly_api_key",
        "poly_passphrase",
        "poly_signature",
        "poly_address",
        "authorization",
        "cookie",
    }
)

RETRYABLE_STATUS: Final = frozenset({500, 502, 503, 504})
STATUS_RATE_LIMITED: Final = 429
STATUS_CLIENT_ERROR: Final = 400


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Redaction happens here, at the transport, so no call site can forget."""
    return {
        key: ("<redacted>" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


@dataclass
class TokenBucket:
    """Token bucket with a burst ceiling, matching how both venues describe throttling.

    `capacity` is the burst allowance and `refill_per_second` the sustained budget.
    `take` blocks until the tokens are available — blocking is the correct behaviour for
    a medium-frequency system, because the alternative is discovering the limit from the
    other side as a 429.
    """

    capacity: float
    refill_per_second: float
    _tokens: float = 0.0
    _last_refill: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ValueError("token bucket capacity and refill rate must be positive")
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._last_refill = now

    def take(self, cost: float = 1.0, *, sleep: Callable[[float], None] = time.sleep) -> float:
        """Consume `cost` tokens, waiting if necessary. Returns seconds waited."""
        if cost <= 0:
            raise ValueError("token cost must be positive")
        if cost > self.capacity:
            raise ValueError(
                f"request costs {cost} tokens but the bucket holds at most {self.capacity}; "
                "this request can never be sent under the configured limit"
            )
        self._refill(time.monotonic())
        if self._tokens >= cost:
            self._tokens -= cost
            return 0.0
        deficit = cost - self._tokens
        wait = deficit / self.refill_per_second
        sleep(wait)
        self._refill(time.monotonic())
        self._tokens = max(0.0, self._tokens - cost)
        return wait


class HttpTransport:
    """A rate-limited HTTP client with a strict retry policy."""

    def __init__(
        self,
        base_url: str,
        *,
        read_bucket: TokenBucket,
        write_bucket: TokenBucket | None = None,
        timeout: float = 10.0,
        max_read_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.read_bucket = read_bucket
        self.write_bucket = write_bucket or read_bucket
        self.max_read_retries = max_read_retries
        self._sleep = sleep
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        token_cost: float = 1.0,
    ) -> Any:
        """Idempotent read, decoded as JSON."""
        response = self.get_response(path, params=params, headers=headers, token_cost=token_cost)
        return self._body(response, "GET", path)

    def get_response(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        token_cost: float = 1.0,
    ) -> httpx.Response:
        """Idempotent read, returning the whole response.

        Callers that need a response *header* — notably the venue clock, which is read
        from the HTTP `Date` header rather than a guessed JSON field — use this.
        Retried with exponential backoff on transport errors and 5xx.
        """
        attempt = 0
        while True:
            self.read_bucket.take(token_cost, sleep=self._sleep)
            try:
                response = self._client.get(
                    path, params=dict(params or {}), headers=dict(headers or {})
                )
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > self.max_read_retries:
                    raise VenueUnavailable(
                        f"GET {path} failed after {attempt} attempts: {exc}"
                    ) from exc
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code in RETRYABLE_STATUS:
                attempt += 1
                if attempt > self.max_read_retries:
                    raise VenueUnavailable(
                        f"GET {path} returned {response.status_code} after {attempt} attempts"
                    )
                self._sleep(self._backoff(attempt))
                continue

            self._raise_for_status(response, "GET", path)
            return response

    def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        token_cost: float = 1.0,
    ) -> Any:
        """Non-idempotent write. **Never retried.**

        A timeout here means the request's outcome is unknown, not that it failed. The
        caller must query the venue for the true state before deciding anything — which
        is exactly what the reconciler and the crash-recovery path are for.
        """
        self.write_bucket.take(token_cost, sleep=self._sleep)
        try:
            response = self._client.post(path, json=dict(json), headers=dict(headers or {}))
        except httpx.HTTPError as exc:
            raise VenueUnavailable(
                f"POST {path} failed with {type(exc).__name__}: the request may or may not "
                "have been executed; query venue state before retrying"
            ) from exc
        self._raise_for_status(response, "POST", path)
        return self._body(response, "POST", path)

    def delete(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        token_cost: float = 1.0,
    ) -> Any:
        """Cancel. Not retried, for the same reason as POST."""
        self.write_bucket.take(token_cost, sleep=self._sleep)
        try:
            response = self._client.delete(
                path, params=dict(params or {}), headers=dict(headers or {})
            )
        except httpx.HTTPError as exc:
            raise VenueUnavailable(
                f"DELETE {path} failed with {type(exc).__name__}: outcome unknown"
            ) from exc
        self._raise_for_status(response, "DELETE", path)
        return self._body(response, "DELETE", path)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(16.0, 2.0**attempt)

    @staticmethod
    def _raise_for_status(response: httpx.Response, method: str, path: str) -> None:
        status = response.status_code
        if status == STATUS_RATE_LIMITED:
            raise VenueRateLimited(
                f"{method} {path} was rate limited; our budget accounting is wrong"
            )
        if status in (401, 403):
            raise VenueAuthError(
                f"{method} {path} returned {status}. Check clock skew before credentials — "
                "both venues reject signatures whose timestamp has drifted."
            )
        if status >= STATUS_CLIENT_ERROR:
            raise VenueError(f"{method} {path} returned {status}: {response.text[:200]}")

    @staticmethod
    def _body(response: httpx.Response, method: str, path: str) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise VenueDataError(
                f"{method} {path} returned a non-JSON body: {response.text[:200]}"
            ) from exc

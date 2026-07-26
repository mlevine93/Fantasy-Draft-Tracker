"""Time.

Kalshi signs with Unix **milliseconds**; Polymarket signs with Unix **seconds**. Both
reject a signature whose timestamp has drifted too far from their clock, and the failure
presents as a 401 that looks exactly like a credential problem. That is the 3am bug this
module exists to prevent: there is one clock, it tracks measured skew per venue, and the
caller has to name the unit it wants.

Nothing here calls the network. Skew is *supplied* by the venue adapters from the venues'
own time endpoints; this module only stores and applies it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

__all__ = ["Clock", "StaleTimestampError", "utc_now"]

#: Beyond this, we assume the host clock is wrong rather than the venue, and halt.
#: Signing with a clock this far off produces auth failures that retries cannot fix.
MAX_TOLERATED_SKEW: Final = timedelta(seconds=5)


class StaleTimestampError(RuntimeError):
    """A timestamp was too old to be used, or clock skew exceeded tolerance."""


def utc_now() -> datetime:
    """The only permitted source of wall-clock time. Always timezone-aware UTC."""
    return datetime.now(UTC)


class Clock:
    """Wall-clock time, corrected by per-venue measured skew."""

    def __init__(self) -> None:
        self._skew: dict[str, timedelta] = {}

    def observe_venue_time(self, venue: str, venue_time: datetime) -> timedelta:
        """Record the offset between a venue's clock and ours. Returns the skew.

        Raises rather than silently correcting when the offset is implausible: a
        multi-second skew means something is wrong with the host, and quietly papering
        over it would let us sign orders with timestamps we cannot reason about.
        """
        if venue_time.tzinfo is None:
            raise ValueError(f"venue_time for {venue} must be timezone-aware")
        skew = venue_time.astimezone(UTC) - utc_now()
        if abs(skew) > MAX_TOLERATED_SKEW:
            raise StaleTimestampError(
                f"clock skew vs {venue} is {skew.total_seconds():.3f}s, "
                f"tolerance is {MAX_TOLERATED_SKEW.total_seconds()}s"
            )
        self._skew[venue] = skew
        return skew

    def now_for(self, venue: str) -> datetime:
        """Current time as the venue believes it to be."""
        return utc_now() + self._skew.get(venue, timedelta(0))

    def epoch_millis_for(self, venue: str) -> int:
        """Unix milliseconds. Kalshi's signing unit."""
        return int(self.now_for(venue).timestamp() * 1000)

    def epoch_seconds_for(self, venue: str) -> int:
        """Unix seconds. Polymarket's signing unit."""
        return int(self.now_for(venue).timestamp())

    def skew_for(self, venue: str) -> timedelta:
        return self._skew.get(venue, timedelta(0))


def require_fresh(timestamp: datetime, *, max_age: timedelta, what: str) -> None:
    """Raise if `timestamp` is older than `max_age`, or is in the future.

    Used on quotes before sizing and on approvals before execution. A future-dated
    timestamp is treated as an error, not as maximally fresh — it means a clock is
    wrong somewhere, and trusting it would defeat the staleness check entirely.
    """
    if timestamp.tzinfo is None:
        raise ValueError(f"{what} timestamp must be timezone-aware")
    age = utc_now() - timestamp.astimezone(UTC)
    if age < -MAX_TOLERATED_SKEW:
        raise StaleTimestampError(f"{what} is dated {-age} in the future; check clocks")
    if age > max_age:
        raise StaleTimestampError(
            f"{what} is {age.total_seconds():.1f}s old, "
            f"max is {max_age.total_seconds():.1f}s"
        )

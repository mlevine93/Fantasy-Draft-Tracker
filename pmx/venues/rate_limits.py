"""Per-venue rate limit budgets.

Separate module for a specific reason: these are the only numbers in `pmx/venues` that
are legitimately floats — seconds and token counts, never money. Keeping them here lets
the float-literal guard in tests/test_single_order_path.py stay strict about every module
that parses a price, instead of being weakened venue by venue until it catches nothing.

Both budgets are guesses on the pessimistic side. Kalshi's tiers are third-party sourced
(docs/api-notes.md §1.3) and no Polymarket limit was reachable at all. We are not
latency-sensitive (§12): being slow costs nothing, being banned costs everything.
"""

from __future__ import annotations

from typing import Final

from pmx.venues.transport import TokenBucket

__all__ = ["kalshi_read_bucket", "polymarket_read_bucket"]

#: Assume Kalshi's Basic tier: ~20 reads/sec sustained, with ~2s of burst.
KALSHI_READ_BURST: Final = 20.0
KALSHI_READ_PER_SECOND: Final = 10.0

#: No published figure was reachable. Start slow and raise it once observed.
POLYMARKET_READ_BURST: Final = 10.0
POLYMARKET_READ_PER_SECOND: Final = 5.0


def kalshi_read_bucket() -> TokenBucket:
    """A fresh bucket per client: buckets are stateful and must not be shared."""
    return TokenBucket(capacity=KALSHI_READ_BURST, refill_per_second=KALSHI_READ_PER_SECOND)


def polymarket_read_bucket() -> TokenBucket:
    return TokenBucket(capacity=POLYMARKET_READ_BURST, refill_per_second=POLYMARKET_READ_PER_SECOND)

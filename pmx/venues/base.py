"""The venue interface.

Adding a third venue must not touch strategy or risk code, so everything above this
line speaks only the canonical model. Two separate interfaces, deliberately:

`MarketDataVenue` is what Phase 1 implements and what strategies are allowed to see.
`TradingVenue` extends it with the order methods, and **no implementation of it exists
yet**. Declaring it now fixes the shape that `execution/router.py` will be the only
caller of — the AST test in tests/test_single_order_path.py already enforces that rule
against a method set this file defines.

Venue clients never see a `Signal`, a `Decision`, or the risk config. They translate
between wire formats and the canonical model, and nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pmx.core.models import Market, OutcomeRef, ProposedOrder, Quote, Venue
from pmx.core.money import Usd

__all__ = [
    "MarketDataVenue",
    "TradingVenue",
    "VenueAuthError",
    "VenueDataError",
    "VenueError",
    "VenueRateLimited",
    "VenueUnavailable",
]


class VenueError(RuntimeError):
    """Base for anything that went wrong talking to a venue."""


class VenueAuthError(VenueError):
    """Signature or credentials rejected. Usually a clock, not a key — see core/clock.py."""


class VenueRateLimited(VenueError):
    """The venue throttled us. Treated as a circuit event, never as a retry hint."""


class VenueUnavailable(VenueError):
    """Transport failure, 5xx, or timeout. The request's outcome may be unknown."""


class VenueDataError(VenueError):
    """A response did not match the schema we expect.

    Raised rather than coerced. A field we cannot parse is a field whose meaning we do
    not know, and guessing at it is how a price ends up off by a factor of 100. This
    matters more than usual right now: the response schemas in this package were written
    from SDK source rather than from live documentation (docs/api-notes.md §0), so this
    exception is the mechanism by which a wrong guess surfaces loudly instead of
    silently producing a plausible number.
    """


class MarketDataVenue(ABC):
    """Read-only market access. Safe for strategies and the recorder to hold."""

    venue: Venue

    @abstractmethod
    def server_time(self) -> datetime:
        """The venue's own clock, used to measure skew. Timezone-aware UTC."""

    @abstractmethod
    def list_markets(
        self, *, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[Market], str | None]:
        """One page of open markets, plus the cursor for the next page."""

    @abstractmethod
    def get_market(self, market_key: str) -> Market:
        """One market by its venue-native key."""

    @abstractmethod
    def get_quote(self, outcome: OutcomeRef) -> Quote:
        """Current book for one outcome. Always `QuoteSource.BOOK`."""

    @abstractmethod
    def close(self) -> None:
        """Release transport resources."""


class TradingVenue(MarketDataVenue):
    """Order and account access.

    **Not implemented in Phase 1.** No subclass of this exists; the risk engine is the
    only thing that may produce an order and `execution/router.py` will be the only
    thing that may call these methods.
    """

    @abstractmethod
    def balance(self) -> Usd:
        """Settled cash at the venue, per the venue, not per our books."""

    @abstractmethod
    def place_order(self, order: ProposedOrder) -> str:
        """Submit an order and return the venue's id for it. Router only.

        The order's idempotency key must travel to the venue: it is how recovery finds
        out whether an order exists when the response was lost.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an order. Router only."""

    @abstractmethod
    def open_orders(self) -> list[Any]:
        """Live orders per the venue. The reconciler's source of truth."""

    @abstractmethod
    def positions(self) -> list[Any]:
        """Positions per the venue. The reconciler's source of truth."""

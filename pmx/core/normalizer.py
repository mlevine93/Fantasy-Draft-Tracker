"""Canonical-model invariants, checked at the venue boundary.

Venue clients do the translation; this module states what the result must satisfy before
anything downstream is allowed to use it. Keeping the checks here rather than inside each
client means a third venue inherits them by construction instead of by review.

The checks are deliberately about the properties that cost money when violated: a price
that cannot size a trade, a market with no correlation tag, a book whose ordering is
inverted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pmx.core.clock import utc_now
from pmx.core.models import Market, Quote, QuoteSource, Side

__all__ = [
    "NormalizationError",
    "require_correlation_tags",
    "require_sizeable",
    "require_sorted_book",
]


class NormalizationError(ValueError):
    """A venue payload parsed, but the result violates a canonical invariant."""


def require_sizeable(
    quote: Quote, side: Side, *, max_age: timedelta, now: datetime | None = None
) -> None:
    """Raise unless this quote may be used to size a trade on `side`.

    The risk engine checks all of this again — that is the point, it is the gate — but
    strategies and the recorder should not be handling unsizeable quotes at all, and a
    failure here names the reason before the signal is ever constructed.
    """
    if quote.source is not QuoteSource.BOOK:
        raise NormalizationError(
            f"{quote.outcome} price came from {quote.source}; only BOOK may size a trade"
        )
    if quote.touch(side) is None:
        raise NormalizationError(f"{quote.outcome} has no {side} price on the book")
    if quote.visible_quantity(side) <= 0:
        raise NormalizationError(f"{quote.outcome} has no visible {side} depth")

    age = (now or utc_now()) - quote.observed_at
    if age > max_age:
        raise NormalizationError(
            f"{quote.outcome} quote is {age.total_seconds():.1f}s old, max is "
            f"{max_age.total_seconds():.1f}s"
        )


def require_sorted_book(quote: Quote) -> None:
    """Raise unless both sides are best-price-first.

    Every depth calculation downstream assumes a taker eats the top of the array. An
    inverted book does not error anywhere — it silently reports the *worst* visible
    price as the touch, which passes a slippage check it should have failed.
    """
    bid_prices = [level.price for level in quote.bid_depth]
    if bid_prices != sorted(bid_prices, reverse=True):
        raise NormalizationError(f"{quote.outcome} bid depth is not descending: {bid_prices}")

    ask_prices = [level.price for level in quote.ask_depth]
    if ask_prices != sorted(ask_prices):
        raise NormalizationError(f"{quote.outcome} ask depth is not ascending: {ask_prices}")

    if quote.bid_depth and quote.bid is not None and quote.bid_depth[0].price != quote.bid:
        raise NormalizationError(
            f"{quote.outcome} best bid {quote.bid} does not match top of book "
            f"{quote.bid_depth[0].price}"
        )
    if quote.ask_depth and quote.ask is not None and quote.ask_depth[0].price != quote.ask:
        raise NormalizationError(
            f"{quote.outcome} best ask {quote.ask} does not match top of book "
            f"{quote.ask_depth[0].price}"
        )


def require_correlation_tags(market: Market) -> None:
    """Raise if a market carries no correlation tag.

    The risk engine treats an untagged market as correlated with its entire venue, which
    is safe but blunt. Catching it here, at ingestion, is what keeps that fallback from
    quietly becoming the normal case and choking off every position on the venue.
    """
    if not market.correlation_tags:
        raise NormalizationError(
            f"{market.venue}:{market.market_key} has no correlation tags; it would be "
            "treated as correlated with the whole venue"
        )

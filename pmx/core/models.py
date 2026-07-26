"""Canonical domain model. Venue-agnostic, frozen, `Decimal`-only.

Everything crossing a venue boundary is converted into these types by a normalizer and
never converted back except at the moment of order placement. Strategies and the risk
engine see only this vocabulary — they cannot reach a venue-native price, a cent, or a
float, because none is reachable from here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pmx.core.money import Probability, Usd, capital_at_risk

__all__ = [
    "Fill",
    "Market",
    "OrderState",
    "OutcomeRef",
    "PromotionState",
    "ProposedOrder",
    "Quote",
    "QuoteSource",
    "Side",
    "Signal",
    "TimeInForce",
    "Venue",
]


class Frozen(BaseModel):
    """Base for every domain object: immutable, strict, no surprise coercion."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TimeInForce(StrEnum):
    GTC = "gtc"
    FOK = "fok"
    FAK = "fak"


class QuoteSource(StrEnum):
    """Where a price came from, and therefore what it may be used for."""

    #: Live order book. The only source permitted to size a trade.
    BOOK = "book"
    #: Metadata/analytics API (e.g. Polymarket Gamma). Known to lag the book.
    #: The risk engine rejects any signal sized off one of these.
    INDICATIVE = "indicative"


class OrderState(StrEnum):
    #: Written to the ledger before the request leaves the process. If we crash here,
    #: recovery must query the venue before assuming anything.
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    #: Submitted, outcome unknown after an ambiguous timeout. Never retried blind.
    UNKNOWN = "unknown"


class PromotionState(StrEnum):
    """§6 gate. The router refuses signals from anything not LIVE."""

    BACKTEST = "backtest"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"
    #: Tripped its own loss limit. Disables itself without halting the system.
    DISABLED = "disabled"


class OutcomeRef(Frozen):
    """Points at one tradable outcome on one venue.

    `outcome_key` is whatever the venue's *order* endpoint takes — a Polymarket token ID,
    or a Kalshi ticker plus side. `market_key` and `event_key` exist for aggregation:
    correlated exposure is computed on `event_key`, never on `market_key`, because ten
    markets on one event are one bet.
    """

    venue: Venue
    market_key: str = Field(min_length=1)
    outcome_key: str = Field(min_length=1)
    event_key: str = Field(min_length=1)

    def __str__(self) -> str:
        return f"{self.venue}:{self.outcome_key}"


class Market(Frozen):
    venue: Venue
    market_key: str = Field(min_length=1)
    event_key: str = Field(min_length=1)
    question: str = Field(min_length=1)
    close_time: datetime
    resolution_source: str = Field(min_length=1)
    #: Free-form tags naming the underlying real-world event. Positions sharing any tag
    #: are aggregated for `max_correlated_exposure`. Empty is legal but punished: the
    #: risk engine treats an untagged market as correlated with its whole venue.
    correlation_tags: tuple[str, ...] = ()
    tick_size: Decimal = Decimal("0.01")
    #: 24h traded notional, used by `max_illiquid_pct`. None means unknown, which is
    #: treated as illiquid rather than as liquid.
    volume_24h: Usd | None = None

    @field_validator("close_time")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("close_time must be timezone-aware (UTC)")
        return value


class DepthLevel(Frozen):
    price: Probability
    quantity: int = Field(gt=0)


class Quote(Frozen):
    outcome: OutcomeRef
    bid: Probability | None
    ask: Probability | None
    #: Visible depth, best price first. Used for `max_order_size_pct_of_book`.
    bid_depth: tuple[DepthLevel, ...] = ()
    ask_depth: tuple[DepthLevel, ...] = ()
    observed_at: datetime
    source: QuoteSource

    @field_validator("observed_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _crossed_book(self) -> Quote:
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"crossed book: bid {self.bid} > ask {self.ask}")
        return self

    def visible_quantity(self, side: Side) -> int:
        """Total visible contracts on the side we would be taking from."""
        levels = self.ask_depth if side is Side.BUY else self.bid_depth
        return sum(level.quantity for level in levels)

    def touch(self, side: Side) -> Probability | None:
        """Best price available to a taker on this side."""
        return self.ask if side is Side.BUY else self.bid


class Signal(Frozen):
    """A strategy's opinion. Never an instruction — only the risk engine makes those."""

    signal_id: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    outcome: OutcomeRef
    side: Side
    #: What the strategy believes the true probability is.
    thesis_price: Probability
    #: The worst price it is willing to trade at.
    limit_price: Probability
    #: Upper bound on size the strategy wants. Sizing may reduce it, never raise it.
    max_quantity: Annotated[int, Field(gt=0)]
    #: Human-readable reason, written to the audit ledger verbatim.
    rationale: str = Field(min_length=1)
    created_at: datetime
    #: The quote this signal was formed against. Staleness and source are checked here.
    quote: Quote
    #: Market orders are opt-in per signal (§4). Default is limit-only.
    allow_market_order: bool = False

    @field_validator("created_at")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return value

    @model_validator(mode="after")
    def _quote_matches_outcome(self) -> Signal:
        if self.quote.outcome != self.outcome:
            raise ValueError(
                f"signal outcome {self.outcome} does not match quote outcome {self.quote.outcome}"
            )
        return self

    @model_validator(mode="after")
    def _limit_consistent_with_thesis(self) -> Signal:
        """A buy limit above the thesis price is a trade with negative expected value
        before fees have even been considered. It is a strategy bug, not a signal."""
        if self.side is Side.BUY and self.limit_price > self.thesis_price:
            raise ValueError(
                f"buy limit {self.limit_price} exceeds thesis {self.thesis_price}: negative edge"
            )
        if self.side is Side.SELL and self.limit_price < self.thesis_price:
            raise ValueError(
                f"sell limit {self.limit_price} below thesis {self.thesis_price}: negative edge"
            )
        return self

    def raw_edge(self) -> Decimal:
        """Signed edge in probability points, before fees, spread, and buffer."""
        if self.side is Side.BUY:
            return self.thesis_price.edge_over(self.limit_price)
        return self.limit_price.edge_over(self.thesis_price)


class ProposedOrder(Frozen):
    """What the risk engine emits when a signal survives. The only input to execution."""

    idempotency_key: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    outcome: OutcomeRef
    side: Side
    limit_price: Probability
    quantity: Annotated[int, Field(gt=0)]
    time_in_force: TimeInForce = TimeInForce.GTC
    #: Worst-case cost including the fee estimate that cleared the edge test.
    max_cost: Usd
    estimated_fee: Usd
    #: Kelly diagnostics, logged so the operator can see how often the cap binds.
    kelly_raw_quantity: int
    kelly_capped_quantity: int
    created_at: datetime

    @model_validator(mode="after")
    def _cost_covers_notional(self) -> ProposedOrder:
        floor = capital_at_risk(
            self.limit_price, self.quantity, is_short=self.side is Side.SELL
        )
        if self.max_cost < floor:
            raise ValueError(f"max_cost {self.max_cost} below notional {floor}")
        return self


class Fill(Frozen):
    fill_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    outcome: OutcomeRef
    side: Side
    quantity: Annotated[int, Field(gt=0)]
    price: Probability
    #: What the venue actually charged.
    fee_charged: Usd
    #: What our fee model predicted. Divergence between these two halts trading —
    #: it is the only continuous check we have on a fee schedule we could not verify.
    fee_modeled: Usd
    filled_at: datetime


class Position(Frozen):
    outcome: OutcomeRef
    quantity: int
    average_price: Probability
    realized_pnl: Usd = Usd.zero()
    last_reconciled_at: datetime | None = None

    def cost_basis(self) -> Usd:
        return capital_at_risk(self.average_price, abs(self.quantity), is_short=self.quantity < 0)

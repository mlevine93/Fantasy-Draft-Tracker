"""Strategy interface.

Strategies are the least trusted component in the system, so the interface is built to
make them harmless: `on_tick` receives market state and an abstract "capital available"
number, and returns *signals*. It has no credentials, no venue client, no order type in
scope, and no way to learn the account balance beyond the single figure it is handed.

A strategy cannot place an order. It cannot size one either — it proposes a maximum and
the risk engine decides. That asymmetry is the design: the component most likely to be
wrong is the one with the least authority.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from pmx.core.models import Market, PromotionState, Quote, Signal
from pmx.core.money import Usd

__all__ = ["Strategy", "StrategyContext"]


class StrategyContext:
    """Everything a strategy is allowed to know."""

    def __init__(self, capital_available: Usd, promotion_state: PromotionState) -> None:
        #: An abstract budget, not the account balance. Deliberately not equity: a
        #: strategy that can see the bankroll can be written to scale into losses.
        self.capital_available = capital_available
        self.promotion_state = promotion_state


class Strategy(ABC):
    """Pure-ish: given market state, return signals."""

    name: str

    @abstractmethod
    def on_tick(
        self, markets: Sequence[Market], quotes: Sequence[Quote], context: StrategyContext
    ) -> list[Signal]:
        """Return zero or more signals. Never places anything, never sees credentials."""

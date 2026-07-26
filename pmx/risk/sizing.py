"""Position sizing: fractional Kelly, hard-capped.

Kelly is optimal given a *known* edge. We do not have one — we have an estimate from a
strategy that has not been proven, priced against a market that may know more than we do.
So Kelly is used for its shape (bet more when the edge is bigger and the price is lower)
and then overruled by a flat cap whenever it gets ambitious.

Both numbers are returned and both are logged. How often the cap binds is the most
honest available measure of how much the strategy's confidence exceeds its evidence.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from pydantic import BaseModel, ConfigDict

from pmx.core.models import Side
from pmx.core.money import Probability, Usd, notional

__all__ = ["Sizing", "kelly_fraction_for", "size_position"]


class Sizing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Contracts full Kelly would buy at this price and belief.
    kelly_raw_quantity: int
    #: After the fractional-Kelly multiplier and every cap.
    quantity: int
    #: Which constraint produced `quantity`. "kelly" means nothing bound.
    binding_constraint: str
    kelly_fraction: Decimal
    stake: Usd


def kelly_fraction_for(thesis: Probability, price: Probability, side: Side) -> Decimal:
    """Full-Kelly stake as a fraction of bankroll for a $1-payout binary contract.

    Buying at price p believing the true probability is q: the bet wins (1-p) with
    probability q and loses p with probability (1-q), giving f* = (q - p) / (1 - p).
    Selling is the same expression on the complement, f* = (p - q) / p.

    Returns 0 when the edge is non-positive; a negative Kelly means "take the other
    side", never "take this side smaller", and acting on it here would silently invert
    the strategy's intent.
    """
    q = thesis.value
    p = price.value

    if side is Side.BUY:
        if p >= 1:
            return Decimal(0)
        fraction = (q - p) / (Decimal(1) - p)
    else:
        if p <= 0:
            return Decimal(0)
        fraction = (p - q) / p

    return fraction if fraction > 0 else Decimal(0)


def size_position(
    *,
    thesis: Probability,
    price: Probability,
    side: Side,
    bankroll: Usd,
    kelly_multiplier: Decimal,
    max_position_value: Usd,
    max_position_pct_of_bankroll: Decimal,
    signal_max_quantity: int,
) -> Sizing:
    """Contracts to trade, and which constraint decided it.

    Every cap is applied as a `min` over quantities, so no cap can ever be escaped by
    the ordering of the checks — the result is the tightest of them by construction.
    """
    full_kelly = kelly_fraction_for(thesis, price, side)

    if full_kelly <= 0 or price.value <= 0 or bankroll <= Usd.zero():
        return Sizing(
            kelly_raw_quantity=0,
            quantity=0,
            binding_constraint="no_edge" if full_kelly <= 0 else "no_bankroll",
            kelly_fraction=full_kelly,
            stake=Usd.zero(),
        )

    # Cost per contract is the price itself (a contract at p costs $p and can lose $p).
    cost_per_contract = price.value

    def contracts_for(stake: Usd) -> int:
        return int((stake.amount / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR))

    raw_stake = bankroll * full_kelly
    kelly_raw_quantity = contracts_for(raw_stake)

    candidates: list[tuple[int, str]] = [
        (contracts_for(bankroll * (full_kelly * kelly_multiplier)), "kelly"),
        (contracts_for(max_position_value), "max_position_size"),
        (
            contracts_for(bankroll * max_position_pct_of_bankroll),
            "max_position_pct_of_bankroll",
        ),
        (signal_max_quantity, "signal_max_quantity"),
    ]

    quantity, binding = min(candidates, key=lambda pair: (pair[0], pair[1]))

    return Sizing(
        kelly_raw_quantity=kelly_raw_quantity,
        quantity=max(quantity, 0),
        binding_constraint=binding,
        kelly_fraction=full_kelly,
        stake=notional(price, max(quantity, 0)),
    )

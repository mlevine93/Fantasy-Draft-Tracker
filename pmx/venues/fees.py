"""Fee model.

Fees are a *function*, not a constant, because both venues have changed their schedules
and Polymarket's V2 reportedly sets the rate at match time rather than in the signed
order. More importantly: as of 2026-07-26 neither schedule could be verified from primary
sources (docs/api-notes.md §0), and an underestimated fee does not shrink an edge — it
invents one, and the system then trades that phantom edge repeatedly and confidently.

Three defenses, in order of how much they are worth:

1. `verified=false` in the config makes every signal on that venue a hard reject.
2. `safety_multiplier` biases every estimate upward, so error costs opportunity, not money.
3. `assert_fee_model_matches()` compares modeled against charged on every real fill.
   That is the one that actually validates the model, because it uses the venue's own
   arithmetic as the oracle.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from pmx.core.models import Venue
from pmx.core.money import Probability, Usd
from pmx.risk.limits import FeeModelConfig, RiskConfig

__all__ = ["FeeDivergence", "FeeModel", "assert_fee_model_matches"]


class FeeDivergence(RuntimeError):
    """Modeled fee and charged fee disagree. Halt: our cost model is wrong."""


class FeeModel:
    """Estimates trading fees for one venue from configured coefficients."""

    def __init__(self, venue: Venue, config: FeeModelConfig) -> None:
        self.venue = venue
        self.config = config

    @property
    def verified(self) -> bool:
        return self.config.verified

    def estimate(self, price: Probability, quantity: int, *, is_maker: bool) -> Usd:
        """Worst-case fee for `quantity` contracts at `price`.

        Follows the parabolic per-contract shape both venues' published schedules use
        (peaking at 0.50, falling toward the extremes), rounds **up** to the cent as the
        venue does, applies the per-contract cap, then applies the safety multiplier.
        Rounding up is not conservatism for its own sake: the ceiling is exactly what
        makes small orders disproportionately expensive and kills marginal edges, and a
        model that rounds to nearest would systematically miss that.
        """
        if quantity <= 0:
            raise ValueError(f"fee quantity must be positive, got {quantity}")

        coefficient = self.config.maker_coefficient if is_maker else self.config.taker_coefficient
        per_contract = coefficient * price.value * (Decimal(1) - price.value)
        capped = min(per_contract, self.config.per_contract_cap.amount)

        gross = (capped * quantity).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        return Usd(gross * self.config.safety_multiplier)

    def round_trip_estimate(
        self, entry: Probability, exit_price: Probability, quantity: int, *, is_maker: bool
    ) -> Usd:
        """Both legs. Anything held to resolution pays only the entry leg — but a
        strategy that may need to unwind must clear both, so callers choose."""
        return self.estimate(entry, quantity, is_maker=is_maker) + self.estimate(
            exit_price, quantity, is_maker=is_maker
        )


def fee_models(config: RiskConfig) -> dict[Venue, FeeModel]:
    return {venue: FeeModel(venue, model) for venue, model in config.fee_models.items()}


def assert_fee_model_matches(modeled: Usd, charged: Usd, *, tolerance: Usd) -> None:
    """Raise if the venue charged materially more than we modeled.

    Asymmetric on purpose. Charging *less* than modeled is safe — we were conservative,
    which is the intended bias. Charging more means our cost model is wrong in the
    direction that manufactures phantom edge, and every edge calculation the system has
    made is suspect. That is a halt, not a log line.
    """
    if charged > modeled + tolerance:
        raise FeeDivergence(
            f"venue charged {charged} but model predicted {modeled} "
            f"(tolerance {tolerance}); fee model is wrong in the dangerous direction"
        )

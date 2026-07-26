"""Money and probability primitives.

The rule from the brief is "no float in money math, anywhere". Enforcing that by
remembering to write `Decimal(...)` is not enforcement — it works until the one call
site nobody reviewed. So the primitives themselves refuse floats at construction and
refuse arithmetic against foreign types. A float reaching an order is a `TypeError`
at the boundary it crosses, not a rounding error discovered in reconciliation.

Two units exist:

  Probability  a price, in [0, 1]. What both venues quote, once normalized.
  Usd          an amount of money.

They do not mix implicitly. Turning a price into money requires `notional()`, which
is explicit about the $1-per-contract payout convention that makes the conversion
meaningful in the first place.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = [
    "MoneyTypeError",
    "Probability",
    "Usd",
    "capital_at_risk",
    "cents_to_probability",
    "notional",
    "probability_to_cents",
]

#: Sub-cent precision. Kalshi's fee formula produces fractions of a cent per contract
#: and rounds up at the *aggregate*, so intermediate values must survive below $0.01.
USD_QUANTUM: Final = Decimal("0.00000001")
PROB_QUANTUM: Final = Decimal("0.000000001")

#: A Kalshi contract settles at $1.00, so its price cannot exceed 100 cents.
_MAX_KALSHI_CENTS: Final = 100


class MoneyTypeError(TypeError):
    """A float, or a foreign type, tried to participate in money math."""


def _coerce(value: object, *, unit: str) -> Decimal:
    """Convert to Decimal, refusing anything that could carry binary rounding error."""
    if isinstance(value, bool):
        # bool is an int subclass; silently treating True as 1 dollar is not a thing
        # we want to be possible.
        raise MoneyTypeError(f"bool is not a valid {unit} value")
    if isinstance(value, float):
        raise MoneyTypeError(
            f"float is not permitted in money math: {unit}({value!r}). "
            "Pass a Decimal, int, or str."
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyTypeError(f"{unit} must be finite, got {value}")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise MoneyTypeError(f"cannot parse {unit} from {value!r}") from exc
        if not parsed.is_finite():
            raise MoneyTypeError(f"{unit} must be finite, got {value!r}")
        return parsed
    raise MoneyTypeError(f"{type(value).__name__} is not a valid {unit} value")


class Usd:
    """An exact amount of US dollars. Immutable."""

    __slots__ = ("_v",)
    _v: Decimal

    def __init__(self, value: Decimal | int | str) -> None:
        object.__setattr__(self, "_v", _coerce(value, unit="Usd").quantize(USD_QUANTUM))

    # -- construction -------------------------------------------------------
    @classmethod
    def zero(cls) -> Usd:
        return cls(0)

    @classmethod
    def _validate(cls, value: object) -> Usd:
        return value if isinstance(value, cls) else cls(value)  # type: ignore[arg-type]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )

    # -- access -------------------------------------------------------------
    @property
    def amount(self) -> Decimal:
        return self._v

    def to_cents_ceil(self) -> int:
        """Round up to whole cents. Used for fees: the venue rounds against us."""
        return int(self._v.quantize(Decimal("0.01"), rounding=ROUND_CEILING) * 100)

    def to_cents_floor(self) -> int:
        return int(self._v.quantize(Decimal("0.01"), rounding=ROUND_FLOOR) * 100)

    def is_zero(self) -> bool:
        return self._v == 0

    # -- arithmetic ---------------------------------------------------------
    def __add__(self, other: Usd) -> Usd:
        if not isinstance(other, Usd):
            raise MoneyTypeError(f"cannot add {type(other).__name__} to Usd")
        return Usd(self._v + other._v)

    def __sub__(self, other: Usd) -> Usd:
        if not isinstance(other, Usd):
            raise MoneyTypeError(f"cannot subtract {type(other).__name__} from Usd")
        return Usd(self._v - other._v)

    def __mul__(self, other: int | Decimal) -> Usd:
        return Usd(self._v * _coerce(other, unit="multiplier"))

    __rmul__ = __mul__

    def __truediv__(self, other: int | Decimal) -> Usd:
        divisor = _coerce(other, unit="divisor")
        if divisor == 0:
            raise ZeroDivisionError("Usd division by zero")
        return Usd(self._v / divisor)

    def ratio_to(self, other: Usd) -> Decimal:
        """Usd / Usd is a dimensionless ratio, not money."""
        if not isinstance(other, Usd):
            raise MoneyTypeError(f"cannot take ratio of Usd to {type(other).__name__}")
        if other._v == 0:
            raise ZeroDivisionError("Usd ratio with zero denominator")
        return self._v / other._v

    def __neg__(self) -> Usd:
        return Usd(-self._v)

    def __abs__(self) -> Usd:
        return Usd(abs(self._v))

    # -- comparison ---------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Usd) and self._v == other._v

    def __hash__(self) -> int:
        return hash(("Usd", self._v))

    def _cmp_operand(self, other: object, op: str) -> Decimal:
        if not isinstance(other, Usd):
            raise MoneyTypeError(f"cannot compare Usd {op} {type(other).__name__}")
        return other._v

    def __lt__(self, other: object) -> bool:
        return self._v < self._cmp_operand(other, "<")

    def __le__(self, other: object) -> bool:
        return self._v <= self._cmp_operand(other, "<=")

    def __gt__(self, other: object) -> bool:
        return self._v > self._cmp_operand(other, ">")

    def __ge__(self, other: object) -> bool:
        return self._v >= self._cmp_operand(other, ">=")

    # -- display ------------------------------------------------------------
    def __repr__(self) -> str:
        return f"Usd('{self._v.normalize():f}')"

    def __str__(self) -> str:
        return f"${self._v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Usd is immutable")


class Probability:
    """An implied probability in [0, 1]. Also serves as a contract price."""

    __slots__ = ("_v",)
    _v: Decimal

    def __init__(self, value: Decimal | int | str) -> None:
        parsed = _coerce(value, unit="Probability").quantize(PROB_QUANTUM)
        if parsed < 0 or parsed > 1:
            raise ValueError(f"Probability must be in [0, 1], got {parsed}")
        object.__setattr__(self, "_v", parsed)

    @classmethod
    def _validate(cls, value: object) -> Probability:
        return value if isinstance(value, cls) else cls(value)  # type: ignore[arg-type]

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )

    @property
    def value(self) -> Decimal:
        return self._v

    def complement(self) -> Probability:
        """The other side of the same market: P(No) = 1 - P(Yes)."""
        return Probability(Decimal(1) - self._v)

    def edge_over(self, other: Probability) -> Decimal:
        """Signed difference in probability points. Not money, not bps."""
        if not isinstance(other, Probability):
            raise MoneyTypeError(f"cannot take edge of Probability over {type(other).__name__}")
        return self._v - other._v

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Probability) and self._v == other._v

    def __hash__(self) -> int:
        return hash(("Probability", self._v))

    def _cmp_operand(self, other: object, op: str) -> Decimal:
        if not isinstance(other, Probability):
            raise MoneyTypeError(f"cannot compare Probability {op} {type(other).__name__}")
        return other._v

    def __lt__(self, other: object) -> bool:
        return self._v < self._cmp_operand(other, "<")

    def __le__(self, other: object) -> bool:
        return self._v <= self._cmp_operand(other, "<=")

    def __gt__(self, other: object) -> bool:
        return self._v > self._cmp_operand(other, ">")

    def __ge__(self, other: object) -> bool:
        return self._v >= self._cmp_operand(other, ">=")

    def __repr__(self) -> str:
        return f"Probability('{self._v.normalize():f}')"

    def __str__(self) -> str:
        return f"{self._v.normalize():f}"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Probability is immutable")


def notional(price: Probability, quantity: int) -> Usd:
    """Capital at risk for `quantity` contracts bought at `price`.

    Both venues settle at $1 per winning contract, so a contract bought at p costs
    $p and can lose exactly $p. That identity — cost equals maximum loss — is why
    notional is the right input to every exposure limit.
    """
    if not isinstance(price, Probability):
        raise MoneyTypeError(f"notional() price must be Probability, got {type(price).__name__}")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise MoneyTypeError(
            f"notional() quantity must be int contracts, got {type(quantity).__name__}"
        )
    if quantity < 0:
        raise ValueError(f"notional() quantity must be non-negative, got {quantity}")
    return Usd(price.value * quantity)


def capital_at_risk(price: Probability, quantity: int, *, is_short: bool) -> Usd:
    """Maximum loss on a position — the only correct input to an exposure limit.

    Long and short are not symmetric on a $1-settling binary contract. A contract
    bought at p costs p and can lose p. A contract *sold* at p collects p and can lose
    (1 - p), because settlement pays the holder $1 and we owe it. Sizing a short off its
    proceeds understates the risk by (1-p)/p — a 9x understatement on a 10-cent short,
    which is exactly the leg of the book where a strategy is most tempted to sell.
    """
    return notional(price.complement() if is_short else price, quantity)


def cents_to_probability(cents: int) -> Probability:
    """Kalshi's integer-cent quote to canonical probability. Venue boundary only."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise MoneyTypeError(f"cents must be int, got {type(cents).__name__}")
    if not 0 <= cents <= _MAX_KALSHI_CENTS:
        raise ValueError(f"Kalshi price must be 0..100 cents, got {cents}")
    return Probability(Decimal(cents) / 100)


def probability_to_cents(probability: Probability) -> int:
    """Canonical probability back to Kalshi's integer cents. Venue boundary only.

    Rejects prices that are not on Kalshi's 1-cent tick rather than silently rounding:
    a price that cannot be expressed is a bug upstream, and rounding it would place an
    order at a price the strategy never authorized.
    """
    if not isinstance(probability, Probability):
        raise MoneyTypeError(
            f"probability_to_cents() expects Probability, got {type(probability).__name__}"
        )
    scaled = probability.value * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{probability} is not on Kalshi's 1-cent tick")
    return int(scaled)

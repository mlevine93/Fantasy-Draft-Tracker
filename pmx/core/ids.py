"""Identifier types.

Polymarket's condition ID names a *market*; its token ID names one *outcome* of that
market. Both are hex strings, both are ~66 characters, and passing one where the other
belongs produces a well-formed request against the wrong thing. The API notes call this
the #1 integration bug, so it is a type error here rather than a test we hope catches it:
`mypy --strict` rejects a ConditionId where a TokenId is expected, at the call site.

The same reasoning applies to Kalshi's three-level ticker hierarchy, where the risk
consequence is different but worse — using a market ticker where an event ticker belongs
silently under-counts correlated exposure.
"""

from __future__ import annotations

from typing import Final, NewType

__all__ = [
    "ConditionId",
    "EventTicker",
    "IdFormatError",
    "MarketTicker",
    "SeriesTicker",
    "TokenId",
    "condition_id",
    "token_id",
]

# --- Polymarket ---
#: Identifies a market (the question). Never valid as an order target.
ConditionId = NewType("ConditionId", str)
#: Identifies one outcome (Yes or No). This is what order endpoints take.
TokenId = NewType("TokenId", str)

# --- Kalshi ---
#: Identifies one tradable market, e.g. "FED-26MAR-C0.25".
MarketTicker = NewType("MarketTicker", str)
#: Identifies the real-world event several markets resolve against. The minimum
#: granularity at which correlated exposure must be aggregated.
EventTicker = NewType("EventTicker", str)
#: Identifies the recurring family an event belongs to.
SeriesTicker = NewType("SeriesTicker", str)

_HEX66_LEN: Final = 66


class IdFormatError(ValueError):
    """An identifier did not have the shape its type requires."""


def _check_hex(value: str, *, kind: str) -> str:
    if not isinstance(value, str):
        raise IdFormatError(f"{kind} must be a string, got {type(value).__name__}")
    if not value.startswith("0x"):
        raise IdFormatError(f"{kind} must start with 0x, got {value!r}")
    if len(value) != _HEX66_LEN:
        raise IdFormatError(f"{kind} must be {_HEX66_LEN} chars, got {len(value)}: {value!r}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise IdFormatError(f"{kind} is not valid hex: {value!r}") from exc
    return value


def condition_id(value: str) -> ConditionId:
    """Parse a Polymarket condition ID at the JSON boundary."""
    return ConditionId(_check_hex(value, kind="ConditionId"))


def token_id(value: str) -> TokenId:
    """Parse a Polymarket token ID at the JSON boundary.

    Token IDs are decimal uint256 strings in some responses and hex in others; accept
    either, but never accept something that is merely "a string we got from JSON".
    """
    if not isinstance(value, str):
        raise IdFormatError(f"TokenId must be a string, got {type(value).__name__}")
    if not value:
        raise IdFormatError("TokenId must not be empty")
    if value.startswith("0x"):
        return TokenId(_check_hex(value, kind="TokenId"))
    if not value.isdigit():
        raise IdFormatError(f"TokenId must be decimal digits or 0x-hex, got {value!r}")
    return TokenId(value)

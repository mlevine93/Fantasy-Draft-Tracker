"""Limit definitions and configuration.

Two properties matter more than the limits themselves:

1. **No limit has a default.** A missing key in `risk_config.yaml` is a startup failure,
   never an implicit "unlimited". The failure mode this prevents is a config typo
   silently disabling a constraint, which is indistinguishable from having no constraint.
2. **Every limit has a name from `Limit`**, and every rejection carries that name plus the
   numbers that produced it. "Why didn't we take that trade" has to be answerable in one
   line, months later, from the ledger alone.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pmx.core.models import Venue
from pmx.core.money import Usd

__all__ = ["ConfigError", "FeeModelConfig", "Limit", "Rejection", "RiskConfig"]


class ConfigError(RuntimeError):
    """Configuration is missing, malformed, or internally inconsistent."""


class Limit(StrEnum):
    """Every reason an order can fail to exist. One name per check, no generic 'rejected'."""

    # Kill switch and halts
    KILL_SWITCH = "kill_switch"
    SYSTEM_HALTED = "system_halted"
    STRATEGY_DISABLED = "strategy_disabled"
    STRATEGY_NOT_LIVE = "strategy_not_live"
    LIVE_TRADING_DISABLED = "live_trading_disabled"

    # Data quality — checked before anything numeric, because a limit computed off a
    # stale or indicative price is not a limit.
    QUOTE_STALE = "quote_stale"
    QUOTE_SOURCE_NOT_BOOK = "quote_source_not_book"
    NO_BOOK_LIQUIDITY = "no_book_liquidity"
    RECONCILIATION_DIVERGED = "reconciliation_diverged"
    RECONCILIATION_STALE = "reconciliation_stale"
    FEE_MODEL_UNVERIFIED = "fee_model_unverified"

    # Capital
    MAX_TOTAL_DEPLOYED = "max_total_deployed"
    MAX_VENUE_DEPLOYED = "max_venue_deployed"
    MAX_POSITION_SIZE = "max_position_size"
    MAX_POSITION_PCT_OF_BANKROLL = "max_position_pct_of_bankroll"
    MAX_DAILY_NEW_CAPITAL = "max_daily_new_capital"
    MIN_CASH_RESERVE = "min_cash_reserve"

    # Loss
    DAILY_LOSS_HALT = "daily_loss_halt"
    MAX_DRAWDOWN_HALT = "max_drawdown_halt"
    PER_STRATEGY_LOSS_HALT = "per_strategy_loss_halt"

    # Exposure
    MAX_CORRELATED_EXPOSURE = "max_correlated_exposure"
    MAX_SINGLE_EVENT_EXPOSURE = "max_single_event_exposure"
    MAX_ILLIQUID_PCT = "max_illiquid_pct"

    # Execution
    MAX_ORDER_SIZE_PCT_OF_BOOK = "max_order_size_pct_of_book"
    MAX_SLIPPAGE_BPS = "max_slippage_bps"
    MIN_EDGE_BPS_AFTER_FEES = "min_edge_bps_after_fees"
    MAX_ORDERS_PER_MINUTE = "max_orders_per_minute"
    MAX_ORDERS_PER_DAY = "max_orders_per_day"
    MIN_TIME_TO_CLOSE = "min_time_to_close"
    MAX_TIME_TO_CLOSE = "max_time_to_close"
    MARKET_ORDER_NOT_PERMITTED = "market_order_not_permitted"

    # Sizing outcome
    SIZE_ROUNDS_TO_ZERO = "size_rounds_to_zero"
    NO_EDGE = "no_edge"

    # Approval
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_PRICE_MOVED = "approval_price_moved"


class Rejection(BaseModel):
    """Why an order does not exist. Written to the ledger verbatim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: Limit
    detail: str
    #: The inputs that produced the decision, so it can be re-derived later.
    values: dict[str, str] = Field(default_factory=dict)

    def one_line(self) -> str:
        return f"{self.limit}: {self.detail}"


class FeeModelConfig(BaseModel):
    """Per-venue fee model coefficients.

    `verified` is the gate described in docs/api-notes.md §0. While it is false, the
    risk engine rejects every signal on that venue, so an unverified fee schedule cannot
    quietly become a phantom edge. Coefficients default to nothing — they must be stated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified: bool
    source: str = Field(min_length=1)
    #: Kalshi-style parabolic coefficient: fee = coefficient * P * (1-P) per contract.
    taker_coefficient: Decimal
    maker_coefficient: Decimal
    #: Per-contract ceiling, in dollars.
    per_contract_cap: Usd
    #: Added to every fee estimate while `verified` is false, and kept afterwards as a
    #: margin against schedule changes we have not noticed yet.
    safety_multiplier: Decimal

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.taker_coefficient < 0 or self.maker_coefficient < 0:
            raise ValueError("fee coefficients must be non-negative")
        if self.safety_multiplier < 1:
            raise ValueError("fee safety_multiplier must be >= 1 (it may only over-estimate)")
        return self


class RiskConfig(BaseModel):
    """Hard limits. Every field is required; there are no permissive defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- master switches ---
    #: §1.2 — no order-placement path is reachable while this is false.
    live_trading_enabled: bool
    #: Absolute path or repo-relative. Its existence halts everything (§1.3).
    kill_file: str

    # --- capital ---
    max_total_deployed: Usd
    max_venue_deployed: dict[Venue, Usd]
    max_position_size: Usd
    max_position_pct_of_bankroll: Decimal
    max_daily_new_capital: Usd
    min_cash_reserve: Usd

    # --- loss ---
    daily_loss_halt: Usd
    max_drawdown_halt: Decimal
    per_strategy_loss_halt: Usd

    # --- exposure ---
    max_correlated_exposure: Usd
    max_single_event_exposure: Usd
    max_illiquid_pct: Decimal
    illiquid_volume_threshold: Usd

    # --- execution ---
    max_order_size_pct_of_book: Decimal
    max_slippage_bps: int
    min_edge_bps_after_fees: int
    max_orders_per_minute: int
    max_orders_per_day: int
    min_time_to_close_seconds: int
    max_time_to_close_seconds: int
    allow_market_orders: bool

    # --- data quality ---
    max_quote_age_seconds: int
    max_reconciliation_age_seconds: int
    reconciliation_tolerance: Usd

    # --- approval ---
    auto_approve_threshold: Usd
    approval_expiry_seconds: int
    #: An approved price that has moved more than this is a new decision, not an
    #: approved one. A stale approval is a rejected approval (§4).
    approval_max_price_drift: Decimal

    # --- sizing ---
    kelly_fraction: Decimal

    # --- fees ---
    fee_models: dict[Venue, FeeModelConfig]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not 0 < self.kelly_fraction <= 1:
            raise ValueError(f"kelly_fraction must be in (0, 1], got {self.kelly_fraction}")
        if not 0 < self.max_position_pct_of_bankroll <= 1:
            raise ValueError("max_position_pct_of_bankroll must be in (0, 1]")
        if not 0 < self.max_drawdown_halt <= 1:
            raise ValueError("max_drawdown_halt must be in (0, 1]")
        if not 0 < self.max_order_size_pct_of_book <= 1:
            raise ValueError("max_order_size_pct_of_book must be in (0, 1]")
        if not 0 <= self.max_illiquid_pct <= 1:
            raise ValueError("max_illiquid_pct must be in [0, 1]")
        if self.min_time_to_close_seconds >= self.max_time_to_close_seconds:
            raise ValueError("min_time_to_close_seconds must be < max_time_to_close_seconds")
        if self.max_position_size > self.max_total_deployed:
            raise ValueError("max_position_size exceeds max_total_deployed")
        for venue, cap in self.max_venue_deployed.items():
            if cap > self.max_total_deployed:
                raise ValueError(f"max_venue_deployed[{venue}] exceeds max_total_deployed")
        if self.min_edge_bps_after_fees <= 0:
            raise ValueError("min_edge_bps_after_fees must be positive; zero is not a buffer")
        if self.max_orders_per_minute <= 0 or self.max_orders_per_day <= 0:
            raise ValueError("order rate limits must be positive")
        if self.max_orders_per_minute > self.max_orders_per_day:
            raise ValueError("max_orders_per_minute exceeds max_orders_per_day")
        return self

    def venue_cap(self, venue: Venue) -> Usd:
        """Per-venue capital cap. A venue with no configured cap cannot be traded."""
        cap = self.max_venue_deployed.get(venue)
        if cap is None:
            raise ConfigError(f"no max_venue_deployed configured for {venue}")
        return cap

    def fee_model(self, venue: Venue) -> FeeModelConfig:
        model = self.fee_models.get(venue)
        if model is None:
            raise ConfigError(f"no fee_model configured for {venue}")
        return model


def _decimalize(node: Any) -> Any:
    """Convert YAML-parsed floats to Decimal *via str*, or reject them.

    PyYAML turns `0.25` into a float. Accepting that would put binary rounding error
    into a limit, so numbers are re-read from their string form. This is the one place
    a float is allowed to exist, and it does not survive the function.
    """
    if isinstance(node, dict):
        return {key: _decimalize(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_decimalize(item) for item in node]
    if isinstance(node, float):
        return Decimal(str(node))
    return node


def load_risk_config(path: Path | str) -> RiskConfig:
    """Load and validate. Any problem raises `ConfigError`; nothing is inferred."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"risk config not found at {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"risk config at {config_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"risk config at {config_path} must be a mapping")
    try:
        return RiskConfig.model_validate(_decimalize(raw))
    except Exception as exc:
        raise ConfigError(f"risk config at {config_path} is invalid: {exc}") from exc

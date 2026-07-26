"""Shared fixtures. Builders default to a *passing* case so each test can break exactly
one thing — a test that has to construct twenty valid fields to check one limit ends up
testing the builder instead of the limit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from pmx.audit.ledger import Ledger
from pmx.core.models import (
    DepthLevel,
    Market,
    OutcomeRef,
    PromotionState,
    Quote,
    QuoteSource,
    Side,
    Signal,
    Venue,
)
from pmx.core.money import Probability, Usd
from pmx.risk.engine import RiskEngine
from pmx.risk.limits import FeeModelConfig, RiskConfig
from pmx.risk.state import PortfolioSnapshot
from pmx.venues.fees import FeeModel

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def make_fee_model_config(*, verified: bool = True) -> FeeModelConfig:
    return FeeModelConfig(
        verified=verified,
        source="test",
        taker_coefficient=Decimal("0.07"),
        maker_coefficient=Decimal("0.0175"),
        per_contract_cap=Usd("0.035"),
        safety_multiplier=Decimal("1.0"),
    )


@pytest.fixture
def config() -> RiskConfig:
    return RiskConfig(
        live_trading_enabled=True,
        kill_file="KILL_DOES_NOT_EXIST",
        max_total_deployed=Usd("10000"),
        max_venue_deployed={Venue.KALSHI: Usd("8000"), Venue.POLYMARKET: Usd("8000")},
        max_position_size=Usd("1000"),
        max_position_pct_of_bankroll=Decimal("0.20"),
        max_daily_new_capital=Usd("5000"),
        min_cash_reserve=Usd("100"),
        daily_loss_halt=Usd("500"),
        max_drawdown_halt=Decimal("0.15"),
        per_strategy_loss_halt=Usd("250"),
        max_correlated_exposure=Usd("2000"),
        max_single_event_exposure=Usd("2000"),
        max_illiquid_pct=Decimal("0.25"),
        illiquid_volume_threshold=Usd("10000"),
        max_order_size_pct_of_book=Decimal("0.50"),
        max_slippage_bps=100,
        min_edge_bps_after_fees=50,
        max_orders_per_minute=10,
        max_orders_per_day=100,
        min_time_to_close_seconds=900,
        max_time_to_close_seconds=7_776_000,
        allow_market_orders=False,
        max_quote_age_seconds=10,
        max_reconciliation_age_seconds=120,
        reconciliation_tolerance=Usd("0.01"),
        auto_approve_threshold=Usd("500"),
        approval_expiry_seconds=300,
        approval_max_price_drift=Decimal("0.01"),
        kelly_fraction=Decimal("0.25"),
        fee_models={
            Venue.KALSHI: make_fee_model_config(),
            Venue.POLYMARKET: make_fee_model_config(),
        },
    )


@pytest.fixture
def outcome() -> OutcomeRef:
    return OutcomeRef(
        venue=Venue.KALSHI,
        market_key="FED-26SEP-C025",
        outcome_key="FED-26SEP-C025:YES",
        event_key="FED-26SEP",
    )


@pytest.fixture
def market(outcome: OutcomeRef) -> Market:
    return Market(
        venue=outcome.venue,
        market_key=outcome.market_key,
        event_key=outcome.event_key,
        question="Will the Fed cut rates at the September 2026 meeting?",
        close_time=NOW + timedelta(days=30),
        resolution_source="FOMC statement",
        correlation_tags=("fed-september-2026",),
        volume_24h=Usd("250000"),
    )


@pytest.fixture
def quote(outcome: OutcomeRef) -> Quote:
    return Quote(
        outcome=outcome,
        bid=Probability("0.38"),
        ask=Probability("0.40"),
        bid_depth=(DepthLevel(price=Probability("0.38"), quantity=5000),),
        ask_depth=(DepthLevel(price=Probability("0.40"), quantity=5000),),
        observed_at=NOW,
        source=QuoteSource.BOOK,
    )


@pytest.fixture
def signal(outcome: OutcomeRef, quote: Quote) -> Signal:
    return Signal(
        signal_id="sig-001",
        strategy="manual",
        outcome=outcome,
        side=Side.BUY,
        thesis_price=Probability("0.55"),
        limit_price=Probability("0.40"),
        max_quantity=100,
        rationale="Test signal with a large, obvious edge.",
        created_at=NOW,
        quote=quote,
    )


@pytest.fixture
def snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Usd("10000"),
        cash=Usd("10000"),
        peak_equity=Usd("10000"),
        deployed_total=Usd.zero(),
        last_reconciled_at=NOW - timedelta(seconds=5),
        reconciliation_ok=True,
        strategy_states={"manual": PromotionState.LIVE},
    )


@pytest.fixture
def engine(config: RiskConfig) -> RiskEngine:
    return RiskEngine(
        config,
        fee_models={
            str(Venue.KALSHI): FeeModel(Venue.KALSHI, config.fee_models[Venue.KALSHI]),
            str(Venue.POLYMARKET): FeeModel(Venue.POLYMARKET, config.fee_models[Venue.POLYMARKET]),
        },
    )


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    with Ledger(tmp_path / "audit.db") as led:
        yield led

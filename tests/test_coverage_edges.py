"""Branch-coverage completion for `pmx/risk/` (§9: 100% branch coverage, non-negotiable).

These are the cases the behavioural suites do not reach: degenerate prices, empty
inputs, invariant violations that only a caller bug could produce. They are real
assertions about real behaviour — none of them exist purely to move a number.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmx.core.models import PromotionState, Side, Venue
from pmx.core.money import Probability, Usd
from pmx.risk.circuit import (
    Halt,
    HaltScope,
    assert_kill_switch_clear,
    evaluate_breakers,
    kill_switch_engaged,
)
from pmx.risk.engine import Decision, DecisionOutcome
from pmx.risk.limits import ConfigError, Limit, Rejection, _decimalize, load_risk_config
from pmx.risk.sizing import kelly_fraction_for, size_position
from pmx.risk.state import HaltStore, PortfolioSnapshot
from tests.conftest import NOW


class TestDecisionShapeInvariants:
    def test_rejected_decision_must_carry_a_rejection(self) -> None:
        with pytest.raises(ValueError, match="must carry a rejection"):
            Decision(outcome=DecisionOutcome.REJECTED, signal_id="s")

    def test_rejected_decision_must_not_carry_an_order(self, engine, signal, market, snapshot):
        approved = engine.evaluate(signal, market, snapshot, now=NOW)
        with pytest.raises(ValueError, match="never carry an order"):
            Decision(
                outcome=DecisionOutcome.REJECTED,
                signal_id="s",
                rejection=Rejection(limit=Limit.NO_EDGE, detail="d"),
                order=approved.order,
            )

    def test_approved_decision_must_carry_an_order(self) -> None:
        with pytest.raises(ValueError, match="must carry an order"):
            Decision(outcome=DecisionOutcome.APPROVED, signal_id="s")

    def test_pending_decision_must_carry_expiry_and_quoted_price(
        self, engine, signal, market, snapshot
    ) -> None:
        approved = engine.evaluate(signal, market, snapshot, now=NOW)
        with pytest.raises(ValueError, match="expiry"):
            Decision(
                outcome=DecisionOutcome.PENDING_APPROVAL, signal_id="s", order=approved.order
            )

    def test_approved_property(self, engine, signal, market, snapshot) -> None:
        assert engine.evaluate(signal, market, snapshot, now=NOW).approved

    def test_rejection_one_line_is_readable(self) -> None:
        rejection = Rejection(limit=Limit.MAX_TOTAL_DEPLOYED, detail="would be $9,999")
        assert rejection.one_line() == "max_total_deployed: would be $9,999"


class TestKellyDegenerateCases:
    def test_buying_at_certainty_has_no_kelly(self) -> None:
        """A contract at 1.00 cannot appreciate; Kelly's odds term is undefined there."""
        assert kelly_fraction_for(Probability("1"), Probability("1"), Side.BUY) == Decimal(0)

    def test_selling_at_zero_has_no_kelly(self) -> None:
        assert kelly_fraction_for(Probability("0"), Probability("0"), Side.SELL) == Decimal(0)

    def test_selling_a_certainty_is_full_kelly(self) -> None:
        # Selling at 1.00 something you believe is worth 0: f = (1-0)/1 = 1.
        assert kelly_fraction_for(Probability("0"), Probability("1"), Side.SELL) == Decimal(1)

    def test_zero_bankroll_sizes_to_nothing(self) -> None:
        sizing = size_position(
            thesis=Probability("0.60"),
            price=Probability("0.40"),
            side=Side.BUY,
            bankroll=Usd.zero(),
            kelly_multiplier=Decimal("0.25"),
            max_position_value=Usd("100"),
            max_position_pct_of_bankroll=Decimal("0.2"),
            signal_max_quantity=100,
        )
        assert sizing.quantity == 0
        assert sizing.binding_constraint == "no_bankroll"

    def test_no_edge_reports_no_edge_not_no_bankroll(self) -> None:
        sizing = size_position(
            thesis=Probability("0.40"),
            price=Probability("0.40"),
            side=Side.BUY,
            bankroll=Usd("1000"),
            kelly_multiplier=Decimal("0.25"),
            max_position_value=Usd("100"),
            max_position_pct_of_bankroll=Decimal("0.2"),
            signal_max_quantity=100,
        )
        assert sizing.quantity == 0
        assert sizing.binding_constraint == "no_edge"

    def test_a_free_contract_is_sized_without_dividing_by_zero(self) -> None:
        sizing = size_position(
            thesis=Probability("0.50"),
            price=Probability("0"),
            side=Side.BUY,
            bankroll=Usd("1000"),
            kelly_multiplier=Decimal("0.25"),
            max_position_value=Usd("100"),
            max_position_pct_of_bankroll=Decimal("0.2"),
            signal_max_quantity=100,
        )
        assert sizing.quantity == 0


class TestBreakerEdges:
    def _kwargs(self, **overrides):
        base = {
            "equity": Usd("10000"),
            "peak_equity": Usd("10000"),
            "day_pnl": Usd.zero(),
            "strategy_pnl": {},
            "last_reconciled_at": NOW - timedelta(seconds=5),
            "reconciliation_ok": True,
            "now": NOW,
        }
        base.update(overrides)
        return base

    def test_zero_peak_equity_cannot_trip_drawdown(self, config) -> None:
        """A fresh account has no peak, so there is nothing to have fallen from."""
        halts = evaluate_breakers(
            config, **self._kwargs(equity=Usd.zero(), peak_equity=Usd.zero())
        )
        assert Limit.MAX_DRAWDOWN_HALT not in [h.limit for h in halts]

    def test_healthy_strategy_does_not_trip(self, config) -> None:
        halts = evaluate_breakers(
            config, **self._kwargs(strategy_pnl={"manual": Usd("100"), "arb": Usd("-10")})
        )
        assert halts == []

    def test_breakers_use_wall_clock_when_now_is_omitted(self, config) -> None:
        from pmx.core.clock import utc_now

        halts = evaluate_breakers(
            config,
            equity=Usd("10000"),
            peak_equity=Usd("10000"),
            day_pnl=Usd.zero(),
            strategy_pnl={},
            last_reconciled_at=utc_now(),
            reconciliation_ok=True,
        )
        assert halts == []

    def test_kill_switch_clear_returns_quietly(self, tmp_path) -> None:
        assert_kill_switch_clear(tmp_path / "KILL")
        assert not kill_switch_engaged(tmp_path / "KILL")


class TestSnapshotAccessors:
    def test_absent_aggregates_read_as_zero(self) -> None:
        snapshot = PortfolioSnapshot(
            equity=Usd("100"), cash=Usd("100"), peak_equity=Usd("100"), deployed_total=Usd.zero()
        )
        assert snapshot.deployed_on(Venue.KALSHI) == Usd.zero()
        assert snapshot.deployed_on_event("nope") == Usd.zero()
        assert snapshot.deployed_on_outcome("nope") == Usd.zero()
        assert snapshot.deployed_on_tag("nope") == Usd.zero()
        assert snapshot.system_halt() is None
        assert snapshot.strategy_halt("manual") is None

    def test_strategy_halt_for_another_strategy_is_not_mine(self) -> None:
        snapshot = PortfolioSnapshot(
            equity=Usd("100"),
            cash=Usd("100"),
            peak_equity=Usd("100"),
            deployed_total=Usd.zero(),
            active_halts=(
                Halt(
                    scope=HaltScope.STRATEGY,
                    limit=Limit.PER_STRATEGY_LOSS_HALT,
                    detail="d",
                    tripped_at=NOW,
                    strategy="calibration",
                ),
            ),
            strategy_states={"manual": PromotionState.LIVE},
        )
        assert snapshot.strategy_halt("manual") is None
        assert snapshot.strategy_halt("calibration") is not None
        assert snapshot.system_halt() is None


class TestStorePaths:
    def test_store_accepts_a_bare_filename(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        with HaltStore("state.db") as store:
            assert store.active() == ()

    def test_ledger_accepts_a_bare_filename(self, tmp_path, monkeypatch) -> None:
        from pmx.audit.ledger import EntryKind, Ledger

        monkeypatch.chdir(tmp_path)
        with Ledger("audit.db") as led:
            led.append(EntryKind.STARTUP, {})
            assert led.verify() == 1


class TestConfigInternals:
    def test_decimalize_handles_nested_lists(self) -> None:
        converted = _decimalize({"a": [0.5, {"b": 0.25}], "c": "x", "d": 3})
        assert converted["a"][0] == Decimal("0.5")
        assert converted["a"][1]["b"] == Decimal("0.25")
        assert converted["c"] == "x"
        assert converted["d"] == 3

    def test_illiquid_pct_above_one_rejected(self, tmp_path) -> None:
        from pathlib import Path

        import yaml

        raw = yaml.safe_load(Path("risk_config.yaml").read_text())
        raw["max_illiquid_pct"] = "1.5"
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ConfigError, match="max_illiquid_pct"):
            load_risk_config(str(path))

    def test_configured_venue_returns_its_caps(self, config) -> None:
        assert config.venue_cap(Venue.KALSHI) == Usd("8000")
        assert config.fee_model(Venue.KALSHI).verified is True

    def test_negative_fee_coefficient_rejected(self, tmp_path) -> None:
        from pathlib import Path

        import yaml

        raw = yaml.safe_load(Path("risk_config.yaml").read_text())
        raw["fee_models"]["kalshi"]["taker_coefficient"] = "-0.01"
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump(raw))
        with pytest.raises(ConfigError):
            load_risk_config(str(path))

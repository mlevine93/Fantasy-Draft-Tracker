"""Configuration loading.

The failure this suite exists to prevent: a config typo that silently disables a limit.
A missing key must be a startup failure, never an implicit "unlimited".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from pmx.core.models import Venue
from pmx.core.money import Usd
from pmx.risk.limits import ConfigError, RiskConfig, load_risk_config

REPO_CONFIG = "risk_config.yaml"


def write(tmp_path, data) -> str:
    path = tmp_path / "risk_config.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


@pytest.fixture
def valid_raw() -> dict:
    return yaml.safe_load(Path(REPO_CONFIG).read_text())


class TestShippedConfig:
    def test_repo_config_loads(self) -> None:
        loaded = load_risk_config(REPO_CONFIG)
        assert isinstance(loaded, RiskConfig)

    def test_repo_config_ships_with_trading_off(self) -> None:
        """§1.2 — the shipped default must not be able to place an order."""
        assert load_risk_config(REPO_CONFIG).live_trading_enabled is False

    def test_repo_config_ships_with_unverified_fee_models(self) -> None:
        """Until the fee schedules are read from primary docs, every venue is gated."""
        config = load_risk_config(REPO_CONFIG)
        assert not any(model.verified for model in config.fee_models.values())

    def test_repo_config_has_a_kill_file(self) -> None:
        assert load_risk_config(REPO_CONFIG).kill_file


class TestMissingKeysAreFatal:
    @pytest.mark.parametrize(
        "key",
        [
            "max_total_deployed",
            "max_position_size",
            "daily_loss_halt",
            "max_drawdown_halt",
            "max_correlated_exposure",
            "min_edge_bps_after_fees",
            "max_orders_per_day",
            "min_cash_reserve",
            "kelly_fraction",
            "live_trading_enabled",
            "kill_file",
        ],
    )
    def test_missing_limit_refuses_to_start(self, tmp_path, valid_raw, key) -> None:
        del valid_raw[key]
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_unknown_key_is_rejected(self, tmp_path, valid_raw) -> None:
        """A typo'd key would otherwise leave the real limit at its default."""
        valid_raw["max_total_deployd"] = "999999"
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_missing_file_is_fatal(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_risk_config("does_not_exist.yaml")

    def test_malformed_yaml_is_fatal(self, tmp_path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("{{{not yaml")
        with pytest.raises(ConfigError):
            load_risk_config(str(path))

    def test_non_mapping_yaml_is_fatal(self, tmp_path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- a\n- b\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_risk_config(str(path))


class TestFloatsFromYaml:
    def test_yaml_floats_become_decimal_not_float(self, tmp_path, valid_raw) -> None:
        """PyYAML parses 0.25 as a float. It must not survive into a limit."""
        valid_raw["kelly_fraction"] = 0.25
        valid_raw["max_drawdown_halt"] = 0.1
        config = load_risk_config(write(tmp_path, valid_raw))
        assert isinstance(config.kelly_fraction, Decimal)
        assert isinstance(config.max_drawdown_halt, Decimal)
        # And exactly 0.1, not 0.1000000000000000055511151231257827.
        assert config.max_drawdown_halt == Decimal("0.1")


class TestIncoherentConfigIsRejected:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("kelly_fraction", "0"),
            ("kelly_fraction", "1.5"),
            ("max_position_pct_of_bankroll", "0"),
            ("max_position_pct_of_bankroll", "1.2"),
            ("max_drawdown_halt", "0"),
            ("max_order_size_pct_of_book", "0"),
            ("min_edge_bps_after_fees", 0),
            ("max_orders_per_minute", 0),
        ],
    )
    def test_out_of_range_values_rejected(self, tmp_path, valid_raw, key, value) -> None:
        valid_raw[key] = value
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_position_cap_above_total_cap_is_incoherent(self, tmp_path, valid_raw) -> None:
        valid_raw["max_position_size"] = "999999"
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_venue_cap_above_total_cap_is_incoherent(self, tmp_path, valid_raw) -> None:
        valid_raw["max_venue_deployed"]["kalshi"] = "999999"
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_time_window_must_be_ordered(self, tmp_path, valid_raw) -> None:
        valid_raw["min_time_to_close_seconds"] = 999_999_999
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_orders_per_minute_above_per_day_is_incoherent(self, tmp_path, valid_raw) -> None:
        valid_raw["max_orders_per_minute"] = 10_000
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))

    def test_fee_safety_multiplier_below_one_is_rejected(self, tmp_path, valid_raw) -> None:
        """A multiplier below 1 would make the fee model optimistic — the one direction
        it is never allowed to be wrong in."""
        valid_raw["fee_models"]["kalshi"]["safety_multiplier"] = "0.5"
        with pytest.raises(ConfigError):
            load_risk_config(write(tmp_path, valid_raw))


class TestAccessors:
    def test_unconfigured_venue_cannot_be_traded(self, config) -> None:
        stripped = config.model_copy(update={"max_venue_deployed": {Venue.KALSHI: Usd("10")}})
        with pytest.raises(ConfigError, match="no max_venue_deployed"):
            stripped.venue_cap(Venue.POLYMARKET)

    def test_unconfigured_fee_model_raises(self, config) -> None:
        stripped = config.model_copy(update={"fee_models": {}})
        with pytest.raises(ConfigError, match="no fee_model"):
            stripped.fee_model(Venue.KALSHI)

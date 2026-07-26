"""Circuit breakers and halt stickiness.

The property that matters: a halt survives a restart. A halt held only in memory is
cleared by the exact action an operator takes when the system stops trading.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from pmx.core.money import Usd
from pmx.risk.circuit import (
    Halt,
    HaltScope,
    KillSwitchEngaged,
    assert_kill_switch_clear,
    drawdown_fraction,
    evaluate_breakers,
    kill_switch_engaged,
)
from pmx.risk.limits import Limit
from pmx.risk.state import HaltStore
from tests.conftest import NOW


class TestKillSwitch:
    def test_absent_file_is_clear(self, tmp_path) -> None:
        assert not kill_switch_engaged(tmp_path / "KILL")

    def test_present_file_engages(self, tmp_path) -> None:
        kill = tmp_path / "KILL"
        kill.write_text("")
        assert kill_switch_engaged(kill)

    def test_empty_file_still_engages(self, tmp_path) -> None:
        """Content is irrelevant. Existence is the whole protocol."""
        kill = tmp_path / "KILL"
        kill.touch()
        with pytest.raises(KillSwitchEngaged):
            assert_kill_switch_clear(kill)

    def test_directory_named_kill_also_engages(self, tmp_path) -> None:
        kill = tmp_path / "KILL"
        kill.mkdir()
        assert kill_switch_engaged(kill)


class TestBreakers:
    def _breakers(self, config, **overrides):
        kwargs = {
            "equity": Usd("10000"),
            "peak_equity": Usd("10000"),
            "day_pnl": Usd.zero(),
            "strategy_pnl": {},
            "last_reconciled_at": NOW - timedelta(seconds=5),
            "reconciliation_ok": True,
            "now": NOW,
        }
        kwargs.update(overrides)
        return evaluate_breakers(config, **kwargs)

    def test_clean_state_trips_nothing(self, config) -> None:
        assert self._breakers(config) == []

    def test_daily_loss_halt(self, config) -> None:
        halts = self._breakers(config, day_pnl=Usd("-501"))
        assert [h.limit for h in halts] == [Limit.DAILY_LOSS_HALT]
        assert halts[0].scope is HaltScope.SYSTEM

    def test_daily_loss_at_exactly_the_limit_does_not_trip(self, config) -> None:
        assert self._breakers(config, day_pnl=Usd("-500")) == []

    def test_drawdown_halt(self, config) -> None:
        halts = self._breakers(config, equity=Usd("8000"), peak_equity=Usd("10000"))
        assert Limit.MAX_DRAWDOWN_HALT in [h.limit for h in halts]

    def test_per_strategy_halt_is_scoped_to_the_strategy(self, config) -> None:
        halts = self._breakers(config, strategy_pnl={"calibration": Usd("-300")})
        assert len(halts) == 1
        assert halts[0].scope is HaltScope.STRATEGY
        assert halts[0].strategy == "calibration"

    def test_all_tripped_breakers_are_returned_not_just_the_first(self, config) -> None:
        halts = self._breakers(
            config,
            day_pnl=Usd("-600"),
            equity=Usd("8000"),
            strategy_pnl={"manual": Usd("-300")},
            reconciliation_ok=False,
        )
        limits = {h.limit for h in halts}
        assert limits == {
            Limit.DAILY_LOSS_HALT,
            Limit.MAX_DRAWDOWN_HALT,
            Limit.PER_STRATEGY_LOSS_HALT,
            Limit.RECONCILIATION_DIVERGED,
        }

    def test_never_reconciled_trips(self, config) -> None:
        halts = self._breakers(config, last_reconciled_at=None)
        assert [h.limit for h in halts] == [Limit.RECONCILIATION_STALE]

    def test_stale_reconciliation_trips(self, config) -> None:
        halts = self._breakers(config, last_reconciled_at=NOW - timedelta(hours=1))
        assert [h.limit for h in halts] == [Limit.RECONCILIATION_STALE]


class TestDrawdownMath:
    def test_no_peak_means_no_drawdown(self) -> None:
        assert drawdown_fraction(Usd("100"), Usd.zero()) == Decimal(0)

    def test_new_high_is_not_a_drawdown(self) -> None:
        assert drawdown_fraction(Usd("120"), Usd("100")) == Decimal(0)

    def test_exact_fraction(self) -> None:
        assert drawdown_fraction(Usd("75"), Usd("100")) == Decimal("0.25")


class TestHaltStickiness:
    def test_halt_survives_a_restart(self, tmp_path) -> None:
        path = tmp_path / "state.db"
        halt = Halt(
            scope=HaltScope.SYSTEM,
            limit=Limit.MAX_DRAWDOWN_HALT,
            detail="drawdown 18%",
            tripped_at=NOW,
        )
        with HaltStore(path) as store:
            store.trip(halt)

        # Restarting the process is what an operator does when trading stops. It must
        # not be a way to resume trading.
        with HaltStore(path) as store:
            active = store.active()
            assert len(active) == 1
            assert active[0].limit is Limit.MAX_DRAWDOWN_HALT

    def test_clearing_requires_an_operator_and_a_reason(self, tmp_path) -> None:
        with HaltStore(tmp_path / "state.db") as store:
            halt_id = store.trip(
                Halt(
                    scope=HaltScope.SYSTEM,
                    limit=Limit.DAILY_LOSS_HALT,
                    detail="d",
                    tripped_at=NOW,
                )
            )
            with pytest.raises(ValueError, match="operator"):
                store.clear(halt_id, operator="  ", note="fine")
            with pytest.raises(ValueError, match="note"):
                store.clear(halt_id, operator="mack", note="")
            assert len(store.active()) == 1

    def test_clearing_works_once(self, tmp_path) -> None:
        with HaltStore(tmp_path / "state.db") as store:
            halt_id = store.trip(
                Halt(
                    scope=HaltScope.SYSTEM,
                    limit=Limit.DAILY_LOSS_HALT,
                    detail="d",
                    tripped_at=NOW,
                )
            )
            assert store.clear(halt_id, operator="mack", note="reviewed fills, cause understood")
            assert store.active() == ()
            # Clearing an already-cleared halt reports False rather than pretending.
            assert not store.clear(halt_id, operator="mack", note="again")

    def test_there_is_no_bulk_clear(self) -> None:
        """Deliberate API absence: clearing is one halt at a time, with a reason."""
        assert not hasattr(HaltStore, "clear_all")

    def test_history_retains_cleared_halts(self, tmp_path) -> None:
        with HaltStore(tmp_path / "state.db") as store:
            halt_id = store.trip(
                Halt(
                    scope=HaltScope.SYSTEM,
                    limit=Limit.DAILY_LOSS_HALT,
                    detail="d",
                    tripped_at=NOW,
                )
            )
            store.clear(halt_id, operator="mack", note="understood")
            history = store.history()
            assert len(history) == 1
            assert history[0]["cleared_by"] == "mack"
            assert history[0]["clear_note"] == "understood"

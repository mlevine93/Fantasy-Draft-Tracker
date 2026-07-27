"""Heartbeat and daily digest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pmx.audit.ledger import EntryKind, Ledger
from pmx.core.money import Usd
from pmx.ops.heartbeat import Heartbeat
from pmx.ops.reporting import build_digest, near_misses_from, render_digest

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class TestHeartbeat:
    def test_missing_file_is_dead_not_unknown(self, tmp_path) -> None:
        """A bot that never started and a bot that died look the same from outside, and
        both mean nobody is watching the open orders."""
        status = Heartbeat(tmp_path / "beat", max_silence=timedelta(minutes=5)).status()
        assert not status.alive
        assert "never run" in status.detail

    def test_fresh_beat_is_alive(self, tmp_path) -> None:
        beat = Heartbeat(tmp_path / "beat", max_silence=timedelta(minutes=5))
        beat.beat("loop ok")
        assert beat.status().alive

    def test_stale_beat_is_dead(self, tmp_path) -> None:
        beat = Heartbeat(tmp_path / "beat", max_silence=timedelta(seconds=30))
        written = beat.beat()
        status = beat.status(now=written + timedelta(minutes=10))
        assert not status.alive
        assert "open orders" in status.detail

    def test_write_is_atomic(self, tmp_path) -> None:
        """Write-then-rename, so a reader never sees a half-written file and concludes
        the process is dead when it is merely mid-write."""
        beat = Heartbeat(tmp_path / "beat", max_silence=timedelta(minutes=5))
        for _ in range(5):
            beat.beat("x")
        assert beat.status().alive
        assert not (tmp_path / "beat.tmp").exists()

    def test_corrupt_file_is_dead_not_alive(self, tmp_path) -> None:
        path = tmp_path / "beat"
        path.write_text("not-a-timestamp\n")
        assert not Heartbeat(path, max_silence=timedelta(minutes=5)).status().alive

    def test_empty_file_is_dead(self, tmp_path) -> None:
        path = tmp_path / "beat"
        path.write_text("")
        assert not Heartbeat(path, max_silence=timedelta(minutes=5)).status().alive


class TestDigest:
    def _ledger(self, tmp_path) -> Ledger:
        return Ledger(tmp_path / "audit.db")

    def test_counts_come_from_the_ledger_not_from_memory(self, tmp_path) -> None:
        """The ledger is what happened. A report built from live state can agree with a
        bug instead of exposing it."""
        with self._ledger(tmp_path) as ledger:
            ledger.append(EntryKind.ORDER_PROPOSED, {"quantity": 10})
            ledger.append(EntryKind.ORDER_SUBMITTED, {"quantity": 10})
            ledger.append(EntryKind.SIGNAL_REJECTED, {"limit": "min_edge_bps_after_fees"})
            ledger.append(EntryKind.SIGNAL_REJECTED, {"limit": "min_edge_bps_after_fees"})
            ledger.append(EntryKind.SIGNAL_REJECTED, {"limit": "max_total_deployed"})

            digest = build_digest(
                ledger,
                datetime.now(UTC).date(),
                equity=Usd("10000"),
                day_pnl=Usd("-25.50"),
            )

        assert digest.orders_proposed == 1
        assert digest.orders_submitted == 1
        assert digest.orders_rejected == 3
        assert list(digest.rejection_reasons) == ["min_edge_bps_after_fees", "max_total_deployed"]

    def test_no_reconciliation_today_is_reported_as_failed(self, tmp_path) -> None:
        """Silence is not success: the risk engine treats stale reconciliation as a hard
        reject, and the digest must say the same thing."""
        with self._ledger(tmp_path) as ledger:
            ledger.append(EntryKind.STARTUP, {})
            digest = build_digest(
                ledger, datetime.now(UTC).date(), equity=Usd("1"), day_pnl=Usd.zero()
            )
        assert not digest.reconciliation_ok
        assert "no reconciliation" in digest.reconciliation_detail

    def test_halts_are_surfaced(self, tmp_path) -> None:
        with self._ledger(tmp_path) as ledger:
            ledger.append(EntryKind.HALT, {"reason": "risk_engine_exception"})
            digest = build_digest(
                ledger, datetime.now(UTC).date(), equity=Usd("1"), day_pnl=Usd.zero()
            )
        assert digest.halts == ("risk_engine_exception",)

    def test_ledger_tampering_appears_in_the_digest(self, tmp_path) -> None:
        """A failed verify is a finding for the report, not a crash that hides it."""
        import sqlite3

        path = tmp_path / "audit.db"
        with Ledger(path) as ledger:
            ledger.append(EntryKind.FILL, {"quantity": 1})
            ledger.append(EntryKind.FILL, {"quantity": 2})

        raw = sqlite3.connect(str(path))
        raw.execute("DROP TRIGGER ledger_no_update")
        raw.execute("UPDATE ledger SET payload = '{}' WHERE seq = 1")
        raw.commit()
        raw.close()

        with Ledger(path) as ledger:
            digest = build_digest(
                ledger, datetime.now(UTC).date(), equity=Usd("1"), day_pnl=Usd.zero()
            )
        assert not digest.ledger_verified
        assert "INTEGRITY FAILURE" in render_digest(digest)

    def test_near_misses_flag_limits_before_they_bind(self, tmp_path) -> None:
        """A limit at 95% is not a rejection yet, and it is the only warning you get."""
        misses = near_misses_from(
            {
                "max_total_deployed": (Usd("9500"), Usd("10000")),
                "max_daily_new_capital": (Usd("100"), Usd("5000")),
            }
        )
        assert [miss.limit for miss in misses] == ["max_total_deployed"]
        assert misses[0].fraction > 0.9

    def test_render_is_plain_text(self, tmp_path) -> None:
        with self._ledger(tmp_path) as ledger:
            ledger.append(EntryKind.ORDER_PROPOSED, {})
            digest = build_digest(
                ledger,
                datetime.now(UTC).date(),
                equity=Usd("10000"),
                day_pnl=Usd("12.34"),
                exposure_by_event={"FED-26SEP": Usd("400")},
                exposure_by_strategy={"manual": Usd("400")},
            )
        text = render_digest(digest)
        assert "PMX daily digest" in text
        assert "$10000.00" in text
        assert "FED-26SEP" in text
        assert "manual" in text

    def test_entries_outside_the_day_are_excluded(self, tmp_path) -> None:
        with self._ledger(tmp_path) as ledger:
            ledger.append(EntryKind.ORDER_PROPOSED, {})
            digest = build_digest(
                ledger,
                datetime.now(UTC).date() - timedelta(days=1),
                equity=Usd("1"),
                day_pnl=Usd.zero(),
            )
        assert digest.orders_proposed == 0
        assert digest.ledger_entries == 0

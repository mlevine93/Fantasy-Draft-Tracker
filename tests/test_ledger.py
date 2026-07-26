"""Audit ledger. Tamper-evidence is the property under test, not append-ability."""

from __future__ import annotations

import sqlite3

import pytest

from pmx.audit.ledger import (
    GENESIS_HASH,
    EntryKind,
    Ledger,
    LedgerIntegrityError,
    canonical_json,
    redact,
)


class TestAppendAndChain:
    def test_first_entry_chains_from_genesis(self, ledger: Ledger) -> None:
        entry = ledger.append(EntryKind.STARTUP, {"version": "0.1.0"})
        assert entry.seq == 1
        assert entry.prev_hash == GENESIS_HASH

    def test_each_entry_carries_the_previous_hash(self, ledger: Ledger) -> None:
        first = ledger.append(EntryKind.STARTUP, {"n": 1})
        second = ledger.append(EntryKind.HEARTBEAT, {"n": 2})
        assert second.prev_hash == first.entry_hash

    def test_verify_walks_a_clean_chain(self, ledger: Ledger) -> None:
        for index in range(25):
            ledger.append(EntryKind.HEARTBEAT, {"n": index})
        assert ledger.verify() == 25

    def test_empty_ledger_verifies(self, ledger: Ledger) -> None:
        assert ledger.verify() == 0


class TestTamperEvidence:
    def test_update_is_rejected_by_the_database(self, ledger: Ledger) -> None:
        ledger.append(EntryKind.FILL, {"quantity": 10})
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger._conn.execute("UPDATE ledger SET payload = '{}' WHERE seq = 1")

    def test_delete_is_rejected_by_the_database(self, ledger: Ledger) -> None:
        ledger.append(EntryKind.FILL, {"quantity": 10})
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            ledger._conn.execute("DELETE FROM ledger WHERE seq = 1")

    def test_edited_content_breaks_the_chain(self, tmp_path) -> None:
        """The triggers stop honest SQL. This is what catches someone with the file."""
        path = tmp_path / "audit.db"
        with Ledger(path) as led:
            led.append(EntryKind.FILL, {"quantity": 10})
            led.append(EntryKind.FILL, {"quantity": 20})

        # Bypass the triggers the way an attacker would: drop them, then edit.
        raw = sqlite3.connect(str(path))
        raw.execute("DROP TRIGGER ledger_no_update")
        raw.execute("UPDATE ledger SET payload = '{\"quantity\":999}' WHERE seq = 1")
        raw.commit()
        raw.close()

        with Ledger(path) as led, pytest.raises(LedgerIntegrityError, match="content tampered"):
            led.verify()

    def test_deleted_row_is_detected_as_a_sequence_gap(self, tmp_path) -> None:
        path = tmp_path / "audit.db"
        with Ledger(path) as led:
            for index in range(3):
                led.append(EntryKind.HEARTBEAT, {"n": index})

        raw = sqlite3.connect(str(path))
        raw.execute("DROP TRIGGER ledger_no_delete")
        raw.execute("DELETE FROM ledger WHERE seq = 2")
        raw.commit()
        raw.close()

        with Ledger(path) as led, pytest.raises(LedgerIntegrityError, match="sequence gap"):
            led.verify()

    def test_rehashed_edit_still_breaks_the_chain(self, tmp_path) -> None:
        """Recomputing the edited row's own hash is not enough — the next row's
        prev_hash still points at the original."""
        from pmx.audit.ledger import compute_hash

        path = tmp_path / "audit.db"
        with Ledger(path) as led:
            led.append(EntryKind.FILL, {"quantity": 10})
            led.append(EntryKind.FILL, {"quantity": 20})

        raw = sqlite3.connect(str(path))
        raw.row_factory = sqlite3.Row
        row = raw.execute("SELECT * FROM ledger WHERE seq = 1").fetchone()
        forged_payload = '{"quantity":999}'
        forged_hash = compute_hash(
            1, str(row["ts"]), str(row["kind"]), forged_payload, str(row["prev_hash"])
        )
        raw.execute("DROP TRIGGER ledger_no_update")
        raw.execute(
            "UPDATE ledger SET payload = ?, hash = ? WHERE seq = 1", (forged_payload, forged_hash)
        )
        raw.commit()
        raw.close()

        with Ledger(path) as led, pytest.raises(LedgerIntegrityError, match="chain broken"):
            led.verify()


class TestRedaction:
    def test_secrets_are_redacted_at_the_writer_not_the_call_site(self, ledger: Ledger) -> None:
        entry = ledger.append(
            EntryKind.CONFIG_CHANGED,
            {"api_key": "pk_live_abcdef", "venue": "kalshi", "passphrase": "hunter2"},
        )
        assert entry.payload["api_key"] == "<redacted>"
        assert entry.payload["passphrase"] == "<redacted>"
        assert entry.payload["venue"] == "kalshi"

    def test_redaction_is_recursive(self) -> None:
        payload = {"creds": {"api_secret": "s3cret", "nested": [{"private_key": "0xdead"}]}}
        cleaned = redact(payload)
        assert cleaned["creds"]["api_secret"] == "<redacted>"
        assert cleaned["creds"]["nested"][0]["private_key"] == "<redacted>"

    def test_secret_never_reaches_disk(self, ledger: Ledger) -> None:
        ledger.append(EntryKind.STARTUP, {"api_secret": "SUPERSECRET"})
        stored = ledger._conn.execute("SELECT payload FROM ledger").fetchone()[0]
        assert "SUPERSECRET" not in stored


class TestSerialization:
    def test_canonical_json_is_stable_across_key_order(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_decimal_serializes_exactly(self) -> None:
        from decimal import Decimal

        assert canonical_json({"x": Decimal("0.1")}) == '{"x":"0.1"}'

    def test_persisted_ledger_reopens_and_continues_the_chain(self, tmp_path) -> None:
        path = tmp_path / "audit.db"
        with Ledger(path) as led:
            first = led.append(EntryKind.STARTUP, {"n": 1})
        with Ledger(path) as led:
            second = led.append(EntryKind.SHUTDOWN, {"n": 2})
            assert second.prev_hash == first.entry_hash
            assert led.verify() == 2

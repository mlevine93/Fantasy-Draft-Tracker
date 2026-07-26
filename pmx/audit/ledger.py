"""Append-only, hash-chained audit ledger.

Every signal, rejection, order, fill, position change, config change, halt and manual
override lands here. Each row carries the hash of the row before it, so any edit or
deletion anywhere in the history invalidates every hash after it and `verify()` says
where. This is the file that goes to the accountant, and — if it ever comes to that —
to a regulator, so the integrity property has to be mechanical rather than procedural.

SQLite triggers reject UPDATE and DELETE on the table. That does not stop someone with
the file and a hex editor; the hash chain is what catches that.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pmx.core.clock import utc_now

__all__ = ["EntryKind", "Ledger", "LedgerEntry", "LedgerIntegrityError"]

GENESIS_HASH: Final = "0" * 64

#: Keys whose values never reach the ledger, matched case-insensitively as substrings.
#: Redaction happens here, in the writer, so no call site can forget.
_SECRET_KEY_PATTERN: Final = re.compile(
    r"(secret|passphrase|private_key|privkey|api_key|password|token|mnemonic|signature)",
    re.IGNORECASE,
)
_REDACTED: Final = "<redacted>"


class LedgerIntegrityError(RuntimeError):
    """The hash chain does not verify. Treat as evidence, not as a bug to work around."""


class EntryKind(StrEnum):
    SIGNAL_RECEIVED = "signal_received"
    SIGNAL_REJECTED = "signal_rejected"
    SIGNAL_APPROVED = "signal_approved"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_EXPIRED = "approval_expired"
    ORDER_PROPOSED = "order_proposed"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_ACKED = "order_acked"
    ORDER_CANCELED = "order_canceled"
    ORDER_UNKNOWN = "order_unknown"
    FILL = "fill"
    POSITION_CHANGED = "position_changed"
    RECONCILIATION = "reconciliation"
    HALT = "halt"
    HALT_CLEARED = "halt_cleared"
    CONFIG_CHANGED = "config_changed"
    MANUAL_OVERRIDE = "manual_override"
    HEARTBEAT = "heartbeat"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"


class LedgerEntry:
    __slots__ = ("entry_hash", "kind", "payload", "prev_hash", "seq", "timestamp")

    def __init__(
        self,
        seq: int,
        timestamp: str,
        kind: str,
        payload: Mapping[str, Any],
        prev_hash: str,
        entry_hash: str,
    ) -> None:
        self.seq = seq
        self.timestamp = timestamp
        self.kind = kind
        self.payload = payload
        self.prev_hash = prev_hash
        self.entry_hash = entry_hash

    def __repr__(self) -> str:
        return f"LedgerEntry(seq={self.seq}, kind={self.kind!r}, hash={self.entry_hash[:12]}...)"


def _json_default(value: object) -> str:
    """Serialize the types our domain model uses. Deliberately no float branch."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def redact(payload: Any) -> Any:
    """Strip credential-shaped values recursively, by key name."""
    if isinstance(payload, Mapping):
        return {
            key: (_REDACTED if _SECRET_KEY_PATTERN.search(str(key)) else redact(value))
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact(item) for item in payload]
    return payload


def canonical_json(payload: Any) -> str:
    """Stable serialization — the hash is only meaningful if the bytes are reproducible."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=_json_default, ensure_ascii=False
    )


def compute_hash(seq: int, timestamp: str, kind: str, payload_json: str, prev_hash: str) -> str:
    material = f"{seq}\x1f{timestamp}\x1f{kind}\x1f{payload_json}\x1f{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS ledger (
    seq        INTEGER PRIMARY KEY,
    ts         TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    prev_hash  TEXT    NOT NULL,
    hash       TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ledger_kind_idx ON ledger(kind);
CREATE INDEX IF NOT EXISTS ledger_ts_idx ON ledger(ts);

CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE rejected');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE rejected');
END;
"""


class Ledger:
    """Single-writer append-only ledger over SQLite in WAL mode."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Durability over throughput: an entry that is not on disk when we crash is an
        # entry describing an order we may have placed.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def head(self) -> tuple[int, str]:
        """(last seq, last hash). (0, GENESIS_HASH) on an empty ledger."""
        row = self._conn.execute(
            "SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row["seq"]), str(row["hash"])

    def append(self, kind: EntryKind, payload: Mapping[str, Any]) -> LedgerEntry:
        """Append one entry. Redaction and hashing are not optional and not skippable."""
        safe_payload = redact(dict(payload))
        payload_json = canonical_json(safe_payload)
        timestamp = utc_now().isoformat()

        # The read of head and the insert must be atomic, or two writers produce two
        # rows claiming the same predecessor and the chain forks silently.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            prev_seq, prev_hash = self.head()
            seq = prev_seq + 1
            entry_hash = compute_hash(seq, timestamp, str(kind), payload_json, prev_hash)
            self._conn.execute(
                "INSERT INTO ledger (seq, ts, kind, payload, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (seq, timestamp, str(kind), payload_json, prev_hash, entry_hash),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return LedgerEntry(seq, timestamp, str(kind), safe_payload, prev_hash, entry_hash)

    def entries(
        self, *, kind: EntryKind | None = None, limit: int | None = None
    ) -> list[LedgerEntry]:
        sql = "SELECT * FROM ledger"
        params: list[Any] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(str(kind))
        sql += " ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._row_to_entry(row) for row in self._conn.execute(sql, params)]

    def iter_entries(self) -> Iterator[LedgerEntry]:
        for row in self._conn.execute("SELECT * FROM ledger ORDER BY seq ASC"):
            yield self._row_to_entry(row)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            seq=int(row["seq"]),
            timestamp=str(row["ts"]),
            kind=str(row["kind"]),
            payload=json.loads(row["payload"]),
            prev_hash=str(row["prev_hash"]),
            entry_hash=str(row["hash"]),
        )

    def verify(self) -> int:
        """Walk the whole chain. Returns entries verified; raises on the first break.

        Recomputes each hash from the stored payload rather than trusting the stored
        hash, so both tampering with content and tampering with hashes are caught.
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        count = 0
        for row in self._conn.execute("SELECT * FROM ledger ORDER BY seq ASC"):
            seq = int(row["seq"])
            if seq != expected_seq:
                raise LedgerIntegrityError(
                    f"sequence gap: expected seq {expected_seq}, found {seq} "
                    "(an entry was deleted or inserted out of order)"
                )
            if str(row["prev_hash"]) != expected_prev:
                raise LedgerIntegrityError(
                    f"chain broken at seq {seq}: prev_hash {row['prev_hash'][:12]}... "
                    f"does not match previous entry hash {expected_prev[:12]}..."
                )
            recomputed = compute_hash(
                seq,
                str(row["ts"]),
                str(row["kind"]),
                str(row["payload"]),
                str(row["prev_hash"]),
            )
            if recomputed != str(row["hash"]):
                raise LedgerIntegrityError(
                    f"content tampered at seq {seq}: stored hash {row['hash'][:12]}... "
                    f"but content hashes to {recomputed[:12]}..."
                )
            expected_prev = str(row["hash"])
            expected_seq += 1
            count += 1
        return count

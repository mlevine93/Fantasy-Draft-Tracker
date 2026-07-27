"""Tick recorder.

Every day without recording is a day of backtest data that cannot be recovered, so this
runs from Phase 1 rather than waiting for a strategy that needs it.

Two decisions worth stating:

**Prices are stored as strings.** Parquet has a decimal type, but the path from Decimal
through pyarrow and back is exactly where a float would sneak in. Strings round-trip
exactly and cost nothing at this data rate.

**The raw payload is stored next to the parsed row.** The response schemas in
`pmx/venues/` were written without access to live documentation (docs/api-notes.md §0).
If one of those guesses is wrong, the parsed columns are wrong too — but the raw JSON
still holds the truth, so the archive can be reparsed rather than thrown away. That is
the difference between discovering a schema error and losing a month of data to it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from pmx.core.models import Quote, Venue

__all__ = ["TickRecorder"]

#: Flush every N rows. Small enough that a crash loses seconds of data, large enough that
#: we are not writing a parquet file per tick.
DEFAULT_BUFFER_ROWS: Final = 500

SCHEMA: Final = pa.schema(
    [
        pa.field("observed_at", pa.timestamp("us", tz="UTC")),
        pa.field("recorded_at", pa.timestamp("us", tz="UTC")),
        pa.field("venue", pa.string()),
        pa.field("market_key", pa.string()),
        pa.field("outcome_key", pa.string()),
        pa.field("event_key", pa.string()),
        pa.field("source", pa.string()),
        # Decimal-as-string, deliberately. See module docstring.
        pa.field("bid", pa.string()),
        pa.field("ask", pa.string()),
        pa.field("bid_size", pa.int64()),
        pa.field("ask_size", pa.int64()),
        pa.field("bid_depth", pa.string()),
        pa.field("ask_depth", pa.string()),
        # The venue's own bytes, so a wrong schema guess costs a reparse, not the data.
        pa.field("raw_payload", pa.string()),
        # Which parser produced this row, so reparsing knows what it is fixing.
        pa.field("parser_version", pa.string()),
    ]
)

PARSER_VERSION: Final = "phase1-unverified-schema"


class TickRecorder:
    """Buffered parquet writer, partitioned by venue and UTC date."""

    def __init__(
        self,
        root: Path | str,
        *,
        buffer_rows: int = DEFAULT_BUFFER_ROWS,
        parser_version: str = PARSER_VERSION,
    ) -> None:
        self.root = Path(root)
        self.buffer_rows = buffer_rows
        self.parser_version = parser_version
        self._buffer: list[dict[str, Any]] = []
        self._writers: dict[Path, pq.ParquetWriter] = {}

    # -- recording ----------------------------------------------------------

    def record(self, quote: Quote, *, raw: Mapping[str, Any] | Sequence[Any] | None = None) -> None:
        self._buffer.append(self._row(quote, raw))
        if len(self._buffer) >= self.buffer_rows:
            self.flush()

    def _row(self, quote: Quote, raw: object) -> dict[str, Any]:
        return {
            "observed_at": quote.observed_at.astimezone(UTC),
            "recorded_at": datetime.now(UTC),
            "venue": str(quote.outcome.venue),
            "market_key": quote.outcome.market_key,
            "outcome_key": quote.outcome.outcome_key,
            "event_key": quote.outcome.event_key,
            "source": str(quote.source),
            "bid": str(quote.bid) if quote.bid is not None else None,
            "ask": str(quote.ask) if quote.ask is not None else None,
            "bid_size": sum(level.quantity for level in quote.bid_depth),
            "ask_size": sum(level.quantity for level in quote.ask_depth),
            "bid_depth": self._encode_depth(quote.bid_depth),
            "ask_depth": self._encode_depth(quote.ask_depth),
            "raw_payload": json.dumps(raw, separators=(",", ":"), default=str)
            if raw is not None
            else None,
            "parser_version": self.parser_version,
        }

    @staticmethod
    def _encode_depth(levels: Sequence[Any]) -> str:
        return json.dumps([[str(level.price), level.quantity] for level in levels],
                          separators=(",", ":"))

    # -- output -------------------------------------------------------------

    def path_for(self, venue: str, day: datetime) -> Path:
        return self.root / venue / day.strftime("%Y-%m-%d") / "ticks.parquet"

    def flush(self) -> int:
        """Write buffered rows. Returns the number written."""
        if not self._buffer:
            return 0

        partitions: dict[Path, list[dict[str, Any]]] = {}
        for row in self._buffer:
            path = self.path_for(row["venue"], row["observed_at"])
            partitions.setdefault(path, []).append(row)

        written = 0
        for path, rows in partitions.items():
            table = pa.Table.from_pylist(rows, schema=SCHEMA)
            writer = self._writers.get(path)
            if writer is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                writer = pq.ParquetWriter(path, SCHEMA, compression="zstd")
                self._writers[path] = writer
            writer.write_table(table)
            written += len(rows)

        self._buffer.clear()
        return written

    def close(self) -> None:
        self.flush()
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    def __enter__(self) -> TickRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading back -------------------------------------------------------

    def read_day(self, venue: Venue | str, day: datetime) -> pa.Table:
        """Read one partition back. Used by the backtester and by schema reparsing."""
        path = self.path_for(str(venue), day)
        if not path.is_file():
            raise FileNotFoundError(f"no recorded ticks at {path}")
        return pq.read_table(path)

"""Tick recorder and normalizer invariants.

Two properties under test: prices survive the round trip exactly, and the venue's raw
bytes are kept alongside the parsed row. The second matters because the parsers in
pmx/venues were written without live documentation — if a schema guess is wrong, the raw
payload is the difference between reparsing an archive and losing it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pmx.core.models import (
    DepthLevel,
    Market,
    OutcomeRef,
    Quote,
    QuoteSource,
    Side,
    Venue,
)
from pmx.core.money import Probability
from pmx.core.normalizer import (
    NormalizationError,
    require_correlation_tags,
    require_sizeable,
    require_sorted_book,
)
from pmx.data.recorder import TickRecorder

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def outcome_ref() -> OutcomeRef:
    return OutcomeRef(
        venue=Venue.KALSHI, market_key="FED-26SEP", outcome_key="FED-26SEP:YES", event_key="FED"
    )


def quote(**overrides) -> Quote:
    fields = {
        "outcome": outcome_ref(),
        "bid": Probability("0.38"),
        "ask": Probability("0.40"),
        "bid_depth": (
            DepthLevel(price=Probability("0.38"), quantity=500),
            DepthLevel(price=Probability("0.37"), quantity=1200),
        ),
        "ask_depth": (DepthLevel(price=Probability("0.40"), quantity=800),),
        "observed_at": NOW,
        "source": QuoteSource.BOOK,
    }
    fields.update(overrides)
    return Quote(**fields)


class TestRecording:
    def test_prices_round_trip_exactly(self, tmp_path) -> None:
        """Stored as strings on purpose: the Decimal->pyarrow->Decimal path is exactly
        where a float would get in."""
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote())

        table = TickRecorder(tmp_path).read_day(Venue.KALSHI, NOW)
        assert table.num_rows == 1
        row = table.to_pylist()[0]
        # Value equality, not string equality: the stored form is normalized fixed
        # point ("0.4" for 0.40), which reparses to the identical Decimal. What must
        # never happen is a value that comes back merely close.
        assert Probability(row["bid"]) == Probability("0.38")
        assert Probability(row["ask"]) == Probability("0.40")
        assert row["bid"] == "0.38"

    def test_depth_is_preserved_with_sizes(self, tmp_path) -> None:
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote())
        row = TickRecorder(tmp_path).read_day(Venue.KALSHI, NOW).to_pylist()[0]
        assert json.loads(row["bid_depth"]) == [["0.38", 500], ["0.37", 1200]]
        assert row["bid_size"] == 1700
        assert row["ask_size"] == 800

    def test_raw_payload_is_kept_beside_the_parsed_row(self, tmp_path) -> None:
        """The insurance policy against an unverified schema guess."""
        raw = {"orderbook": {"yes": [[38, 500]]}, "unknown_field": "keep me"}
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote(), raw=raw)
        row = TickRecorder(tmp_path).read_day(Venue.KALSHI, NOW).to_pylist()[0]
        assert json.loads(row["raw_payload"]) == raw

    def test_rows_carry_the_parser_version(self, tmp_path) -> None:
        """A reparse needs to know which parser produced the row it is correcting."""
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote())
        row = TickRecorder(tmp_path).read_day(Venue.KALSHI, NOW).to_pylist()[0]
        assert row["parser_version"] == "phase1-unverified-schema"

    def test_missing_touch_is_null_not_zero(self, tmp_path) -> None:
        """A zero price is a real price. Absence must stay absent."""
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote(ask=None, ask_depth=()))
        row = TickRecorder(tmp_path).read_day(Venue.KALSHI, NOW).to_pylist()[0]
        assert row["ask"] is None
        assert row["ask_size"] == 0

    def test_partitions_by_venue_and_day(self, tmp_path) -> None:
        other_day = NOW + timedelta(days=1)
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote())
            recorder.record(quote(observed_at=other_day))

        assert (tmp_path / "kalshi" / "2026-07-26" / "ticks.parquet").is_file()
        assert (tmp_path / "kalshi" / "2026-07-27" / "ticks.parquet").is_file()

    def test_buffer_flushes_automatically(self, tmp_path) -> None:
        recorder = TickRecorder(tmp_path, buffer_rows=5)
        for _ in range(5):
            recorder.record(quote())
        # Flushed by the buffer, before close().
        assert (tmp_path / "kalshi" / "2026-07-26" / "ticks.parquet").is_file()
        recorder.close()

    def test_flush_on_empty_buffer_is_a_no_op(self, tmp_path) -> None:
        with TickRecorder(tmp_path) as recorder:
            assert recorder.flush() == 0

    def test_reading_a_missing_day_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            TickRecorder(tmp_path).read_day(Venue.KALSHI, NOW)

    def test_observed_at_not_recorded_at_drives_partitioning(self, tmp_path) -> None:
        """Backtests bucket by when the market said it, not by when we wrote it down."""
        with TickRecorder(tmp_path) as recorder:
            recorder.record(quote(observed_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC)))
        assert (tmp_path / "kalshi" / "2026-01-02" / "ticks.parquet").is_file()


class TestNormalizerInvariants:
    def test_indicative_price_cannot_size(self) -> None:
        with pytest.raises(NormalizationError, match="only BOOK"):
            require_sizeable(
                quote(source=QuoteSource.INDICATIVE),
                Side.BUY,
                max_age=timedelta(seconds=10),
                now=NOW,
            )

    def test_missing_touch_cannot_size(self) -> None:
        with pytest.raises(NormalizationError, match="no buy price"):
            require_sizeable(
                quote(ask=None, ask_depth=()), Side.BUY, max_age=timedelta(seconds=10), now=NOW
            )

    def test_stale_quote_cannot_size(self) -> None:
        with pytest.raises(NormalizationError, match="old"):
            require_sizeable(
                quote(), Side.BUY, max_age=timedelta(seconds=1), now=NOW + timedelta(minutes=5)
            )

    def test_fresh_book_quote_passes(self) -> None:
        require_sizeable(quote(), Side.BUY, max_age=timedelta(seconds=10), now=NOW)

    def test_inverted_bid_depth_is_caught(self) -> None:
        """An inverted book reports the worst price as the touch and quietly passes a
        slippage check it should have failed."""
        inverted = quote(
            bid_depth=(
                DepthLevel(price=Probability("0.30"), quantity=100),
                DepthLevel(price=Probability("0.38"), quantity=100),
            )
        )
        with pytest.raises(NormalizationError, match="not descending"):
            require_sorted_book(inverted)

    def test_inverted_ask_depth_is_caught(self) -> None:
        inverted = quote(
            ask_depth=(
                DepthLevel(price=Probability("0.50"), quantity=100),
                DepthLevel(price=Probability("0.40"), quantity=100),
            )
        )
        with pytest.raises(NormalizationError, match="not ascending"):
            require_sorted_book(inverted)

    def test_touch_must_match_top_of_book(self) -> None:
        with pytest.raises(NormalizationError, match="best bid"):
            require_sorted_book(quote(bid=Probability("0.20")))

    def test_well_formed_book_passes(self) -> None:
        require_sorted_book(quote())

    def test_untagged_market_is_flagged_at_ingestion(self) -> None:
        market = Market(
            venue=Venue.KALSHI,
            market_key="M",
            event_key="E",
            question="Q",
            close_time=NOW,
            resolution_source="s",
        )
        with pytest.raises(NormalizationError, match="correlated with the whole venue"):
            require_correlation_tags(market)

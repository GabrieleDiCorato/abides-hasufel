"""Unit tests for OrderBookSnapshotMemorySink and OrderBookHistoryMemorySink.

These sinks are driven by the EventBus in production but can be tested in
isolation by feeding wire tuples directly to ``on_book_snapshot`` /
``on_event``.  Tests here cover symbol filtering, event-type filtering,
the legacy materialization helpers, and the EventSink Protocol contract.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pytest

from abides_core.sinks.event_sinks import (
    EventSink,
    OrderBookHistoryMemorySink,
    OrderBookSnapshotMemorySink,
)


class _LimitPayload(NamedTuple):
    symbol: str
    order_id: int
    agent_id: int
    side: str
    quantity: int
    price: int


class _ExecPayload(NamedTuple):
    symbol: str
    order_id: int
    agent_id: int
    oppos_order_id: int
    oppos_agent_id: int
    side: str
    quantity: int
    price: int


_BOOK_EVENT_TYPES = frozenset({"LIMIT", "EXEC", "CANCEL"})


# ---------------------------------------------------------------------------
# OrderBookSnapshotMemorySink
# ---------------------------------------------------------------------------


class TestOrderBookSnapshotMemorySink:
    def test_protocol_compliance(self) -> None:
        sink = OrderBookSnapshotMemorySink("ABM", depth=5)
        assert isinstance(sink, EventSink)
        assert sink.accept_book_snapshots is True
        assert sink.accept_events is False
        assert sink.accept_metrics is False

    def test_captures_matching_symbol(self) -> None:
        sink = OrderBookSnapshotMemorySink("ABM", depth=2)
        sink.on_book_snapshot(("ABM", 1000, [(100, 5)], [(101, 7)], 2, 0))
        sink.on_book_snapshot(("ABM", 2000, [(99, 3)], [(102, 4)], 2, 1))
        assert len(sink) == 2

    def test_filters_other_symbols(self) -> None:
        sink = OrderBookSnapshotMemorySink("ABM", depth=2)
        sink.on_book_snapshot(("XYZ", 1000, [(50, 1)], [(51, 1)], 2, 0))
        sink.on_book_snapshot(("ABM", 2000, [(99, 3)], [(102, 4)], 2, 1))
        assert len(sink) == 1

    def test_as_book_log2_matches_legacy_shape(self) -> None:
        sink = OrderBookSnapshotMemorySink("ABM", depth=2)
        sink.on_book_snapshot(("ABM", 1000, [(100, 5)], [(101, 7)], 2, 0))
        sink.on_book_snapshot(("ABM", 2000, [(99, 3)], [(102, 4)], 2, 1))
        out = sink.as_book_log2()
        assert len(out) == 2
        assert out[0]["QuoteTime"] == 1000
        assert isinstance(out[0]["bids"], np.ndarray)
        assert isinstance(out[0]["asks"], np.ndarray)
        np.testing.assert_array_equal(out[0]["bids"], np.array([(100, 5)]))
        np.testing.assert_array_equal(out[1]["asks"], np.array([(102, 4)]))

    def test_lifecycle_hooks_noop(self) -> None:
        sink = OrderBookSnapshotMemorySink("ABM", depth=1)
        # Should not raise.
        sink.on_simulation_start({"sim_id": "test"})
        sink.flush()
        sink.on_simulation_end({"sim_id": "test"})
        assert len(sink) == 0


# ---------------------------------------------------------------------------
# OrderBookHistoryMemorySink
# ---------------------------------------------------------------------------


class TestOrderBookHistoryMemorySink:
    def test_protocol_compliance(self) -> None:
        sink = OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES)
        assert isinstance(sink, EventSink)
        assert sink.accept_events is True
        assert sink.accept_book_snapshots is False
        assert sink.accept_metrics is False

    def test_filters_by_event_type(self) -> None:
        sink = OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES)
        limit = _LimitPayload("ABM", 1, 100, "BID", 5, 10_000)
        sink.on_event((0, "ExchangeAgent", 1000, "LIMIT", limit, 0))
        sink.on_event((0, "ExchangeAgent", 1100, "QUOTE", limit, 1))  # not in allowlist
        assert len(sink) == 1

    def test_filters_by_payload_symbol(self) -> None:
        sink = OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES)
        ours = _LimitPayload("ABM", 1, 100, "BID", 5, 10_000)
        theirs = _LimitPayload("XYZ", 2, 200, "ASK", 5, 10_000)
        sink.on_event((0, "ExchangeAgent", 1000, "LIMIT", ours, 0))
        sink.on_event((0, "ExchangeAgent", 1100, "LIMIT", theirs, 1))
        assert len(sink) == 1

    def test_drops_payload_without_symbol(self) -> None:
        sink = OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES)
        # Plain dict payload without a `.symbol` attribute.
        sink.on_event((0, "ExchangeAgent", 1000, "LIMIT", {"foo": "bar"}, 0))
        assert len(sink) == 0

    def test_as_history_dicts_reconstructs_legacy_shape(self) -> None:
        sink = OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES)
        limit = _LimitPayload("ABM", 7, 42, "BID", 5, 10_000)
        exec_ = _ExecPayload("ABM", 7, 42, 8, 99, "SELL", 3, 10_000)
        sink.on_event((0, "ExchangeAgent", 1000, "LIMIT", limit, 0))
        sink.on_event((0, "ExchangeAgent", 2000, "EXEC", exec_, 1))
        out = sink.as_history_dicts()
        assert len(out) == 2
        assert out[0] == {
            "time": 1000,
            "type": "LIMIT",
            "order_id": 7,
            "agent_id": 42,
            "side": "BID",
            "quantity": 5,
            "price": 10_000,
        }
        # `symbol` must be stripped from the payload.
        assert "symbol" not in out[0]
        assert out[1]["type"] == "EXEC"
        assert out[1]["oppos_agent_id"] == 99
        assert "symbol" not in out[1]
        # Legacy dict ordering: `time` and `type` come first.
        assert list(out[0].keys())[:2] == ["time", "type"]

    def test_entries_returns_raw_tuples(self) -> None:
        sink = OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES)
        limit = _LimitPayload("ABM", 1, 100, "BID", 5, 10_000)
        sink.on_event((0, "ExchangeAgent", 1000, "LIMIT", limit, 0))
        entries = sink.entries()
        assert len(entries) == 1
        assert entries[0][3] == "LIMIT"
        assert entries[0][4] is limit


@pytest.mark.parametrize(
    "sink",
    [
        OrderBookSnapshotMemorySink("ABM", depth=1),
        OrderBookHistoryMemorySink("ABM", _BOOK_EVENT_TYPES),
    ],
)
def test_disabled_callbacks_are_safe_noops(sink: EventSink) -> None:
    """Calls to disabled callbacks must not raise (defensive for tests)."""
    if not sink.accept_events:
        sink.on_event((0, "X", 0, "Y", None, 0))
    if not sink.accept_metrics:
        sink.on_metric((0, "X", 0, "k", 1.0, 0))
    if not sink.accept_book_snapshots:
        sink.on_book_snapshot(("Z", 0, [], [], 1, 0))

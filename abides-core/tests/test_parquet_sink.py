"""Tests for :mod:`abides_core.parquet_sink`.

Covers round-trip equivalence with :class:`InMemorySink`, schema
metadata, the ``__generic__`` fallback, crash recovery via
``.partial/`` skipping, checkpoint rotation, and the clean
``ImportError`` when ``pyarrow`` is unavailable.

All tests use synthetic wire tuples driven directly into the sink,
plus one end-to-end test through ``Kernel`` for parity with
``InMemorySink``.  Tests skip cleanly when pyarrow is not installed
(except the missing-pyarrow test, which patches the import).
"""

from __future__ import annotations

import builtins
import importlib
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from abides_core.event_sinks import InMemorySink  # noqa: E402
from abides_core.kernel import Kernel  # noqa: E402
from abides_core.parquet_sink import (  # noqa: E402
    BUS_FORMAT_VERSION,
    ParquetSink,
    _strip_shard_suffix,
    read_parquet_logs,
    unpickle_payloads,
)
from abides_core.utils import str_to_ns  # noqa: E402

# ---------------------------------------------------------------------------
# Tuple helpers (match the wire format documented in event_records.py)
# ---------------------------------------------------------------------------


def _event(seq: int, *, event_type: str = "AGENT_TYPE", payload: object = None) -> tuple:
    return (1, "TestAgent", 1_000 + seq, event_type, payload, seq)


def _metric(seq: int, *, key: str = "profit", value: float = 1.0) -> tuple:
    return (1, "TestAgent", 1_000 + seq, key, value, seq)


def _book_snapshot(seq: int, *, symbol: str = "ABM") -> tuple:
    bids = ((10_000, 100), (9_990, 50))
    asks = ((10_010, 80), (10_020, 40))
    return (symbol, 1_000 + seq, bids, asks, 2, seq)


def _start_sink(sink: ParquetSink) -> None:
    sink.on_simulation_start({})


def _end_sink(sink: ParquetSink) -> None:
    sink.on_simulation_end({})


# ---------------------------------------------------------------------------
# Construction and dependency handling
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_protocol_compliance(self, tmp_path):
        from abides_core.event_sinks import EventSink

        sink = ParquetSink(root=tmp_path)
        assert isinstance(sink, EventSink)

    def test_invalid_checkpoint_value_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="checkpoint_every_rows"):
            ParquetSink(root=tmp_path, checkpoint_every_rows=0)
        with pytest.raises(ValueError, match="checkpoint_every_rows"):
            ParquetSink(root=tmp_path, checkpoint_every_rows=-1)

    def test_missing_pyarrow_clean_error(self, tmp_path, monkeypatch):
        # Force `import pyarrow` to fail inside the constructor, then
        # re-import the module so the construction path runs through the
        # ImportError branch.
        original_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("pyarrow"):
                raise ImportError("simulated missing pyarrow")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Drop cached pyarrow modules from the constructor's namespace.
        # The constructor imports them at call time, so reloading the
        # module is unnecessary as long as we evict sys.modules entries.
        for key in list(sys.modules):
            if key.startswith("pyarrow"):
                monkeypatch.delitem(sys.modules, key, raising=False)
        # Reload abides_core.parquet_sink so the in-function `import pyarrow`
        # binds against our patched __import__.
        import abides_core.parquet_sink as ps_mod

        importlib.reload(ps_mod)
        with pytest.raises(ImportError, match=r"pip install.*abides-ng\[parquet\]"):
            ps_mod.ParquetSink(root=tmp_path)
        # Restore: reload again under the real __import__.
        monkeypatch.undo()
        importlib.reload(ps_mod)


# ---------------------------------------------------------------------------
# Round-trip: events, metrics, book snapshots
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_event_buckets_per_event_type(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        # Two registered event types + one unknown → 3 keys, one bucket each.
        # Typed columns: AGENT_TYPE has `name` (string), STARTING_CASH has
        # `cents` (int64); the generic fallback keeps the pickled `payload`
        # column plus an extra `event_type` column.
        sink.on_event(_event(0, event_type="AGENT_TYPE", payload="TestAgent"))
        sink.on_event(_event(1, event_type="STARTING_CASH", payload=100_000))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sink.on_event(_event(2, event_type="CUSTOM_THING", payload={"x": 1}))
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1")
        events = data["events"]
        assert set(events.keys()) == {"AGENT_TYPE", "STARTING_CASH", "__generic__"}

        # Typed-Arrow layout: one typed column per PayloadSchema field.
        assert list(events["AGENT_TYPE"].columns) == [
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "name",
            "seq",
        ]
        assert list(events["STARTING_CASH"].columns) == [
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "cents",
            "seq",
        ]
        assert "event_type" in events["__generic__"].columns
        assert "payload" in events["__generic__"].columns

        # Typed values are materialized — no unpickling needed.
        assert events["AGENT_TYPE"]["name"].iloc[0] == "TestAgent"
        assert int(events["STARTING_CASH"]["cents"].iloc[0]) == 100_000

        # Generic bucket payloads still round-trip via pickle.
        generic_df = unpickle_payloads(events["__generic__"])
        assert generic_df["payload"].iloc[0] == {"x": 1}
        assert generic_df["event_type"].iloc[0] == "CUSTOM_THING"

    def test_metric_buckets_per_key(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        sink.on_metric(_metric(0, key="profit", value=42.0))
        sink.on_metric(_metric(1, key="profit", value=43.0))
        sink.on_metric(_metric(2, key="latency_ns", value=1_500.0))
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1", kinds=("metrics",))
        metrics = data["metrics"]
        assert set(metrics.keys()) == {"profit", "latency_ns"}
        assert metrics["profit"]["value"].tolist() == [42.0, 43.0]
        assert metrics["latency_ns"]["value"].iloc[0] == 1_500.0

    def test_book_snapshots_per_symbol(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        sink.on_book_snapshot(_book_snapshot(0, symbol="ABM"))
        sink.on_book_snapshot(_book_snapshot(1, symbol="ABM"))
        sink.on_book_snapshot(_book_snapshot(2, symbol="XYZ"))
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1", kinds=("book_snapshots",))
        books = data["book_snapshots"]
        assert set(books.keys()) == {"ABM", "XYZ"}
        abm = books["ABM"]
        assert len(abm) == 2
        assert abm["depth"].iloc[0] == 2
        # bids/asks round-trip via pickle.
        bids = pickle.loads(abm["bids"].iloc[0])
        assert bids == ((10_000, 100), (9_990, 50))

    def test_holdings_delta_typed_columns(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        # HOLDINGS_DELTA arity ≥ 2 → payload is the positional tuple.
        sink.on_event(
            _event(
                0,
                event_type="HOLDINGS_UPDATED",
                payload=("ABM", 100, 100, 50_000_00),
            )
        )
        sink.on_event(
            _event(
                1,
                event_type="HOLDINGS_UPDATED",
                payload=("ABM", -40, 60, 50_400_00),
            )
        )
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1")
        df = data["events"]["HOLDINGS_UPDATED"]
        assert list(df.columns) == [
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "symbol",
            "delta_qty",
            "qty_after",
            "cash_after_cents",
            "seq",
        ]
        assert df["symbol"].tolist() == ["ABM", "ABM"]
        assert df["delta_qty"].tolist() == [100, -40]
        assert df["qty_after"].tolist() == [100, 60]
        assert df["cash_after_cents"].tolist() == [50_000_00, 50_400_00]

    def test_quote_typed_columns(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        # QUOTE arity 3 → positional tuple (symbol, price_cents, qty).
        sink.on_event(_event(0, event_type="BEST_BID", payload=("ABM", 10_000, 250)))
        sink.on_event(_event(1, event_type="BEST_ASK", payload=("ABM", 10_010, 175)))
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1")
        bid = data["events"]["BEST_BID"]
        ask = data["events"]["BEST_ASK"]
        assert list(bid.columns) == [
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "symbol",
            "price_cents",
            "qty",
            "seq",
        ]
        assert bid["symbol"].iloc[0] == "ABM"
        assert int(bid["price_cents"].iloc[0]) == 10_000
        assert int(bid["qty"].iloc[0]) == 250
        assert int(ask["price_cents"].iloc[0]) == 10_010
        assert int(ask["qty"].iloc[0]) == 175

    def test_empty_payload_no_value_columns(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        # EMPTY arity 0 → payload is the shared () singleton, no value cols.
        sink.on_event(_event(0, event_type="MKT_CLOSED", payload=()))
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1")
        df = data["events"]["MKT_CLOSED"]
        assert list(df.columns) == ["agent_id", "agent_type", "sim_time_ns", "seq"]
        assert len(df) == 1

    def test_order_event_typed_columns(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        # ORDER_EVENT arity 11 → positional tuple.
        payload = (
            42,        # order_id
            "LIMIT",   # order_kind
            "ABM",     # symbol
            1,         # side (IntEnum int repr)
            100,       # quantity
            10_000,    # limit_price
            None,      # stop_price
            0,         # time_in_force
            False,     # is_hidden
            False,     # is_price_to_comply
            "tagA",    # tag
        )
        sink.on_event(_event(0, event_type="ORDER_SUBMITTED", payload=payload))
        _end_sink(sink)

        data = read_parquet_logs(tmp_path / "r1")
        df = data["events"]["ORDER_SUBMITTED"]
        # 3 common + 11 schema + 1 seq = 15 columns.
        assert len(df.columns) == 15
        assert int(df["order_id"].iloc[0]) == 42
        assert int(df["limit_price"].iloc[0]) == 10_000
        assert df["tag"].iloc[0] == "tagA"
        # stop_price is nullable; None round-trips as NaN/None.
        assert pd.isna(df["stop_price"].iloc[0])


# ---------------------------------------------------------------------------
# Schema metadata
# ---------------------------------------------------------------------------


class TestSchemaMetadata:
    def test_bus_format_version_recorded(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        sink.on_event(_event(0))
        _end_sink(sink)

        path = tmp_path / "r1" / "events" / "AGENT_TYPE.parquet"
        md = pq.read_schema(path).metadata
        assert md[b"abides.bus_format_version"] == BUS_FORMAT_VERSION.encode()
        assert md[b"abides.schema_name"] == b"AGENT_TYPE"
        assert md[b"abides.schema_version"] == b"1"

    def test_reader_rejects_incompatible_version(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        sink.on_event(_event(0))
        _end_sink(sink)

        # Rewrite the Parquet file with a bogus bus_format_version.
        path = tmp_path / "r1" / "events" / "AGENT_TYPE.parquet"
        table = pq.read_table(path)
        bad_md = {
            **(table.schema.metadata or {}),
            b"abides.bus_format_version": b"999",
        }
        bad_schema = table.schema.with_metadata(bad_md)
        bad_table = table.cast(bad_schema)
        pq.write_table(bad_table, path)

        with pytest.raises(ValueError, match="bus_format_version"):
            read_parquet_logs(tmp_path / "r1")


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------


class TestGenericFallback:
    def test_unknown_event_type_warns_once(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            sink.on_event(_event(0, event_type="WEIRD", payload="a"))
            sink.on_event(_event(1, event_type="WEIRD", payload="b"))
            sink.on_event(_event(2, event_type="ALSO_WEIRD", payload="c"))
        _end_sink(sink)

        # One warning per distinct unknown type.
        msgs = [str(w.message) for w in caught]
        assert sum("WEIRD" in m and "ALSO_WEIRD" not in m for m in msgs) == 1
        assert sum("ALSO_WEIRD" in m for m in msgs) == 1

        # All three rows land in the same __generic__ file.
        data = read_parquet_logs(tmp_path / "r1")
        generic = data["events"]["__generic__"]
        assert len(generic) == 3
        assert sorted(generic["event_type"].tolist()) == ["ALSO_WEIRD", "WEIRD", "WEIRD"]


# ---------------------------------------------------------------------------
# Crash-recovery: .partial/ files are ignored by the reader
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_partial_files_ignored(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        sink.on_event(_event(0, event_type="AGENT_TYPE", payload="ok"))
        _end_sink(sink)

        # Drop a fake .partial/ file that the reader must ignore.
        partial = tmp_path / "r1" / ".partial" / "events" / "AGENT_TYPE.fake.parquet"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"garbage-not-parquet")

        # Reader does not touch .partial/ at all.
        data = read_parquet_logs(tmp_path / "r1")
        assert list(data["events"].keys()) == ["AGENT_TYPE"]
        assert len(data["events"]["AGENT_TYPE"]) == 1

    def test_stale_partials_wiped_on_restart(self, tmp_path):
        # Pre-create a stale .partial dir.
        stale = tmp_path / "r1" / ".partial" / "events" / "stale.parquet"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")

        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        # Stale partials must be gone after on_simulation_start.
        assert not stale.exists()
        sink.on_event(_event(0))
        _end_sink(sink)


# ---------------------------------------------------------------------------
# Checkpoint rotation
# ---------------------------------------------------------------------------


class TestCheckpointRotation:
    def test_rotation_produces_numbered_shards(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1", checkpoint_every_rows=100)
        _start_sink(sink)
        for seq in range(350):
            sink.on_event(_event(seq, event_type="AGENT_TYPE", payload=f"name_{seq}"))
        _end_sink(sink)

        events_dir = tmp_path / "r1" / "events"
        files = sorted(p.name for p in events_dir.iterdir())
        # Four shards: 0-99, 100-199, 200-299, and the trailing 300-349.
        assert files == [
            "AGENT_TYPE.0-99.parquet",
            "AGENT_TYPE.100-199.parquet",
            "AGENT_TYPE.200-299.parquet",
            "AGENT_TYPE.300-349.parquet",
        ]

        data = read_parquet_logs(tmp_path / "r1")
        df = data["events"]["AGENT_TYPE"]
        # Reader concatenates and sorts by (sim_time_ns, seq).
        assert df["seq"].tolist() == list(range(350))
        assert df["name"].tolist() == [f"name_{i}" for i in range(350)]

    def test_no_checkpoint_writes_single_unnumbered_file(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="r1")
        _start_sink(sink)
        for seq in range(50):
            sink.on_event(_event(seq, event_type="AGENT_TYPE"))
        _end_sink(sink)

        files = sorted(p.name for p in (tmp_path / "r1" / "events").iterdir())
        assert files == ["AGENT_TYPE.parquet"]


# ---------------------------------------------------------------------------
# Shard-suffix stripping
# ---------------------------------------------------------------------------


class TestStripShardSuffix:
    @pytest.mark.parametrize(
        ("stem", "expected"),
        [
            ("FOO", "FOO"),
            ("FOO.0-99", "FOO"),
            ("FOO.100-199", "FOO"),
            ("foo.bar.0-9", "foo.bar"),
            # Trailing dot-suffix without a hyphen is part of the key.
            ("FOO.bar", "FOO.bar"),
            # Non-numeric suffix is part of the key.
            ("FOO.alpha-beta", "FOO.alpha-beta"),
        ],
    )
    def test_strip(self, stem, expected):
        assert _strip_shard_suffix(stem) == expected


# ---------------------------------------------------------------------------
# Coexistence with InMemorySink (end-to-end through Kernel)
# ---------------------------------------------------------------------------


class TestEndToEndWithKernel:
    def test_parity_with_in_memory_sink(self, tmp_path):
        # Tiny kernel run with both sinks registered; row sets must match.
        from abides_core.agent import Agent

        class _TinyAgent(Agent):
            def __init__(self, id: int) -> None:
                super().__init__(
                    id=id,
                    name=f"Tiny_{id}",
                    type="Tiny",
                    random_state=np.random.RandomState(seed=id + 1),
                    log_events=True,
                    log_to_file=False,
                )

            def wakeup(self, current_time):
                super().wakeup(current_time)
                self.logEvent("STARTING_CASH", 12_345)

        in_mem = InMemorySink()
        parquet = ParquetSink(root=tmp_path, run_id="rkernel")
        agent = _TinyAgent(0)
        kernel = Kernel(
            agents=[agent],
            start_time=str_to_ns("09:30:00"),
            stop_time=str_to_ns("16:00:00"),
            skip_log=True,
            event_sinks=[in_mem, parquet],
            random_state=np.random.RandomState(seed=42),
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            kernel.run()

        # Group InMemorySink events by event_type for comparison.
        in_mem_counts: dict[str, int] = {}
        for t in in_mem.events:
            in_mem_counts[t[3]] = in_mem_counts.get(t[3], 0) + 1

        data = read_parquet_logs(tmp_path / "rkernel")
        parquet_counts = {k: len(df) for k, df in data["events"].items()}

        # Known event types must agree exactly; unknown types pool into __generic__.
        from abides_core.event_payloads import EVENT_TYPE_SCHEMA

        known_in_mem = {k: v for k, v in in_mem_counts.items() if k in EVENT_TYPE_SCHEMA}
        unknown_in_mem_total = sum(
            v for k, v in in_mem_counts.items() if k not in EVENT_TYPE_SCHEMA
        )

        for et, count in known_in_mem.items():
            assert parquet_counts.get(et, 0) == count, (
                f"Row count mismatch for {et}: in_mem={count}, "
                f"parquet={parquet_counts.get(et, 0)}"
            )
        if unknown_in_mem_total > 0:
            assert parquet_counts.get("__generic__", 0) == unknown_in_mem_total

        # Seq monotonicity preserved for at least one event type.
        if "AGENT_TYPE" in data["events"]:
            seqs = data["events"]["AGENT_TYPE"]["seq"].tolist()
            assert seqs == sorted(seqs)


# ---------------------------------------------------------------------------
# Empty run: no buckets created
# ---------------------------------------------------------------------------


class TestEmptyRun:
    def test_no_events_no_files(self, tmp_path):
        sink = ParquetSink(root=tmp_path, run_id="empty")
        _start_sink(sink)
        _end_sink(sink)
        data = read_parquet_logs(tmp_path / "empty")
        assert data == {}

    def test_missing_run_dir_returns_empty(self, tmp_path):
        # Reader is forgiving: missing kind dirs and missing run dirs
        # both return an empty mapping.
        result = read_parquet_logs(tmp_path / "does-not-exist")
        assert result == {}

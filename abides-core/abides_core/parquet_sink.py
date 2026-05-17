"""Parquet-backed :class:`~abides_core.event_sinks.EventSink` and reader.

``ParquetSink`` writes per-event-type / per-metric-key / per-symbol
Parquet files to a run directory at simulation end (and optionally at
row-count checkpoints).  The companion :func:`read_parquet_logs` reads
a finished run directory back into in-memory DataFrames.

Layout::

    <root>/<run_id>/
        events/<event_type>.parquet
        metrics/<metric_key>.parquet
        book_snapshots/<symbol>.parquet
        .partial/                 # in-flight files (ignored by reader)

With ``checkpoint_every_rows=N``, files rotate as
``<key>.<seq_lo>-<seq_hi>.parquet`` and the reader concatenates them
by ``seq`` order.

Payload encoding (Phase 2c)
---------------------------

For every event type registered in
:data:`~abides_core.event_payloads.EVENT_TYPE_SCHEMA`, the sink writes
the common columns ``(agent_id, agent_type, sim_time_ns, seq)`` plus
**one typed Arrow column per field in the schema**. Field-to-Arrow
type mapping lives in :data:`_FIELD_TYPE`; arity rules
(0 → no payload cols, 1 → one bare scalar, ≥ 2 → positional tuple
unpacked into N cols) follow :class:`PayloadSchema`.

Unknown event types still fall back to the ``__generic__`` bucket
which carries an extra ``event_type`` column and a pickled
``payload`` column. The order-book ``DEPTH.levels`` field and the
book-snapshot ``bids`` / ``asks`` columns also remain pickled binary
in this phase (typed ``list<struct>`` is a follow-up).

The schema name / version and ``bus_format_version`` are recorded in
the Parquet file's key/value metadata for round-trip validation.

Optional dependency
-------------------

``pyarrow`` is loaded lazily at :class:`ParquetSink` construction.
If it is not installed, the constructor raises :class:`ImportError`
with a hint to ``pip install abides-ng[parquet]``.
"""

from __future__ import annotations

import logging
import os
import pickle
import shutil
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from .event_payloads import EVENT_TYPE_SCHEMA, GENERIC, PayloadSchema

if TYPE_CHECKING:
    import pyarrow as pa

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUS_FORMAT_VERSION = "2"
"""Versions the on-disk layout/contract.

* ``"1"`` — Phase 3 MVP: per-event-type files with one pickled
  ``payload`` column.
* ``"2"`` — Phase 2c: typed columnar layout. Each registered event
  type expands its :class:`PayloadSchema` fields into typed Arrow
  columns; only the ``__generic__`` fallback and the DEPTH /
  book-snapshot list payloads remain pickled.

The reader rejects files whose ``bus_format_version`` metadata does
not match the running library's value.
"""

_META_PREFIX = "abides."
_PARTIAL_DIR = ".partial"
_GENERIC_FILENAME = "__generic__"
_INSTALL_HINT = (
    "pyarrow is required to use ParquetSink. Install it with: "
    "pip install 'abides-ng[parquet]'"
)


# ---------------------------------------------------------------------------
# Field-to-Arrow-type registry
# ---------------------------------------------------------------------------

# Maps a PayloadSchema field name to an Arrow type code. The code is
# resolved against the pyarrow module at schema-build time. Every field
# in every registered schema must appear here; otherwise schema build
# fails at first use with a clear KeyError.
#
# Type codes:
#   "int64"   — integer cents / quantities / counts / nanosecond times
#   "int8"    — IntEnum-backed small-domain integers (Side, TIF, direction)
#   "float64" — fractions / rates / mixed int-or-float values
#   "string"  — symbol / tag / human-readable strings
#   "bool"    — flags
#   "binary"  — pickled fallback (only DEPTH.levels in this phase)
_FIELD_TYPE: dict[str, str] = {
    # ORDER_EVENT
    "order_id": "int64",
    "order_kind": "string",
    "symbol": "string",
    "side": "int8",
    "quantity": "int64",
    "limit_price": "int64",
    "stop_price": "int64",
    "time_in_force": "int8",
    "is_hidden": "bool",
    "is_price_to_comply": "bool",
    "tag": "string",
    # HOLDINGS_DELTA
    "delta_qty": "int64",
    "qty_after": "int64",
    "cash_after_cents": "int64",
    # CASH
    "cents": "int64",
    # DEPTH — pickled fallback (typed list<struct> deferred).
    "levels": "binary",
    # QUOTE
    "price_cents": "int64",
    "qty": "int64",
    # AGENT_TYPE
    "name": "string",
    # SUMMARY
    "text": "string",
    # IMBALANCE
    "bid_total_qty": "int64",
    "ask_total_qty": "int64",
    # FILL_PNL
    "nav": "int64",
    "peak_nav": "int64",
    # VALUATION (mixed int|float surplus / cents)
    "value": "float64",
    # EXECUTION_SUMMARY
    "executed_quantity": "int64",
    "target_quantity": "int64",
    "remaining_quantity": "int64",
    "execution_rate": "float64",
    # SLICE_DECISION
    "time": "int64",
    "order_size": "int64",
    "direction": "int8",
    # POV_SUMMARY
    "effective_pov": "float64",
    "total_market_volume": "int64",
    # AMM_FLATTEN
    "position_closed": "int64",
    # CIRCUIT_BREAKER (value is float64 above; reason here)
    "reason": "string",
    # GENERIC fallback (only used inside the __generic__ bucket)
    "payload": "binary",
}


def _resolve_arrow_type(pa_mod: Any, code: str) -> Any:
    """Resolve a ``_FIELD_TYPE`` code to a pyarrow ``DataType``."""
    if code == "int64":
        return pa_mod.int64()
    if code == "int8":
        return pa_mod.int8()
    if code == "float64":
        return pa_mod.float64()
    if code == "string":
        return pa_mod.string()
    if code == "bool":
        return pa_mod.bool_()
    if code == "binary":
        return pa_mod.binary()
    raise ValueError(f"Unknown Arrow type code {code!r}")


# ---------------------------------------------------------------------------
# Arrow schema builders (lazy, built after pyarrow import succeeds)
# ---------------------------------------------------------------------------


def _build_event_schema(pa_mod: Any, schema: PayloadSchema) -> pa.Schema:
    """Arrow schema for a per-event-type file.

    Common columns ``(agent_id, agent_type, sim_time_ns, seq)`` plus
    one typed column per field in ``schema.fields``. Unknown field
    names raise :class:`KeyError` to surface registry drift early.
    """
    fields = [
        pa_mod.field("agent_id", pa_mod.int64(), nullable=False),
        pa_mod.field("agent_type", pa_mod.string(), nullable=False),
        pa_mod.field("sim_time_ns", pa_mod.int64(), nullable=False),
    ]
    for fname in schema.fields:
        try:
            code = _FIELD_TYPE[fname]
        except KeyError as exc:
            raise KeyError(
                f"ParquetSink: no Arrow type mapping for field {fname!r} "
                f"(schema {schema.name!r}). Add it to _FIELD_TYPE in "
                f"abides_core/parquet_sink.py."
            ) from exc
        fields.append(
            pa_mod.field(fname, _resolve_arrow_type(pa_mod, code), nullable=True)
        )
    fields.append(pa_mod.field("seq", pa_mod.int64(), nullable=False))
    return pa_mod.schema(
        fields,
        metadata={
            f"{_META_PREFIX}bus_format_version": BUS_FORMAT_VERSION,
            f"{_META_PREFIX}schema_name": schema.name,
            f"{_META_PREFIX}schema_version": str(schema.version),
        },
    )


def _build_event_columns(schema: PayloadSchema) -> tuple[str, ...]:
    """Column-name layout for an event bucket (matches the Arrow schema)."""
    return ("agent_id", "agent_type", "sim_time_ns", *schema.fields, "seq")


def _pickle_field_offsets(schema: PayloadSchema) -> tuple[int, ...]:
    """Positions within the payload tuple whose values must be pickled.

    Offsets are 0-indexed into ``schema.fields`` (not into the full row
    tuple). Empty when no field uses the ``binary`` type code.
    """
    return tuple(
        i for i, fname in enumerate(schema.fields)
        if _FIELD_TYPE.get(fname) == "binary"
    )


def _build_generic_event_schema(pa_mod: Any) -> pa.Schema:
    """Arrow schema for the fallback ``__generic__`` events file.

    Carries an extra ``event_type`` column because multiple unknown
    event types are pooled into one file.
    """
    return pa_mod.schema(
        [
            pa_mod.field("agent_id", pa_mod.int64(), nullable=False),
            pa_mod.field("agent_type", pa_mod.string(), nullable=False),
            pa_mod.field("sim_time_ns", pa_mod.int64(), nullable=False),
            pa_mod.field("event_type", pa_mod.string(), nullable=False),
            pa_mod.field("payload", pa_mod.binary(), nullable=True),
            pa_mod.field("seq", pa_mod.int64(), nullable=False),
        ],
        metadata={
            f"{_META_PREFIX}bus_format_version": BUS_FORMAT_VERSION,
            f"{_META_PREFIX}schema_name": GENERIC.name,
            f"{_META_PREFIX}schema_version": str(GENERIC.version),
        },
    )


def _build_metric_schema(pa_mod: Any, key: str) -> pa.Schema:
    """Arrow schema for a per-metric-key file.

    ``value`` is ``float64`` per :data:`~abides_core.event_records.WIRE_FIELDS_METRIC`.
    """
    return pa_mod.schema(
        [
            pa_mod.field("agent_id", pa_mod.int64(), nullable=False),
            pa_mod.field("agent_type", pa_mod.string(), nullable=False),
            pa_mod.field("sim_time_ns", pa_mod.int64(), nullable=False),
            pa_mod.field("value", pa_mod.float64(), nullable=True),
            pa_mod.field("seq", pa_mod.int64(), nullable=False),
        ],
        metadata={
            f"{_META_PREFIX}bus_format_version": BUS_FORMAT_VERSION,
            f"{_META_PREFIX}metric_key": key,
        },
    )


def _build_book_snapshot_schema(pa_mod: Any, symbol: str) -> pa.Schema:
    """Arrow schema for a per-symbol book-snapshot file.

    Phase 3 MVP: ``bids`` / ``asks`` are pickled tuples-of-tuples
    (price_cents, qty).  A typed list-of-struct encoding is a
    follow-up once we drop the pickle column from event payloads.
    """
    return pa_mod.schema(
        [
            pa_mod.field("sim_time_ns", pa_mod.int64(), nullable=False),
            pa_mod.field("bids", pa_mod.binary(), nullable=True),
            pa_mod.field("asks", pa_mod.binary(), nullable=True),
            pa_mod.field("depth", pa_mod.int64(), nullable=False),
            pa_mod.field("seq", pa_mod.int64(), nullable=False),
        ],
        metadata={
            f"{_META_PREFIX}bus_format_version": BUS_FORMAT_VERSION,
            f"{_META_PREFIX}symbol": symbol,
        },
    )


# ---------------------------------------------------------------------------
# Internal buffer
# ---------------------------------------------------------------------------


class _Bucket:
    """Mutable per-key column accumulator.

    Holds parallel Python lists, one per Arrow column.  ``columns`` is
    the column-name list (positional).  ``arrow_schema`` is the
    matching :class:`pyarrow.Schema`.  ``next_seq_lo`` tracks the
    starting ``seq`` of the next checkpoint group; updated on flush.
    """

    __slots__ = (
        "columns",
        "arrow_schema",
        "data",
        "row_count",
        "next_seq_lo",
        "files_written",
    )

    def __init__(self, columns: tuple[str, ...], arrow_schema: pa.Schema) -> None:
        self.columns = columns
        self.arrow_schema = arrow_schema
        # Parallel lists, one per column.
        self.data: list[list[Any]] = [[] for _ in columns]
        self.row_count: int = 0
        self.next_seq_lo: int | None = None
        self.files_written: int = 0

    def append(self, values: tuple[Any, ...]) -> None:
        for col_list, v in zip(self.data, values):
            col_list.append(v)
        self.row_count += 1

    def reset(self) -> None:
        for col_list in self.data:
            col_list.clear()
        self.row_count = 0
        self.next_seq_lo = None


# ---------------------------------------------------------------------------
# ParquetSink
# ---------------------------------------------------------------------------


class ParquetSink:
    """Write events / metrics / book snapshots to per-key Parquet files.

    See module docstring for layout, payload encoding, and crash-safety
    semantics.

    Arguments:
        root:
            Root directory.  Files land under ``<root>/<run_id>/``.
        run_id:
            Subdirectory under ``root`` for this run.  Defaults to
            ``"default"``.  Existing files are **overwritten**.
        compression:
            Parquet compression codec passed to
            :func:`pyarrow.parquet.write_table`.  ``"zstd"`` is the
            recommended default; ``"snappy"`` is faster but larger;
            ``"none"`` disables compression.
        checkpoint_every_rows:
            If set, rotate each bucket to a new file after this many
            rows.  Files are named ``<key>.<seq_lo>-<seq_hi>.parquet``.
            Defaults to ``None`` (one file per key at simulation end).
        accept_events:
            Disable to skip event capture entirely.
        accept_metrics:
            Disable to skip metric capture entirely.
        accept_book_snapshots:
            Disable to skip book-snapshot capture entirely.

    Raises:
        ImportError: ``pyarrow`` is not installed.

    Example::

        from abides_core.parquet_sink import ParquetSink, read_parquet_logs

        sink = ParquetSink(root="logs/", run_id="rmsc04_seed1")
        kernel = Kernel(agents=..., event_sinks=[sink], ...)
        kernel.run()

        data = read_parquet_logs("logs/rmsc04_seed1")
        order_submits = data["events"]["ORDER_SUBMITTED"]
    """

    accept_events: bool = True
    accept_metrics: bool = True
    accept_book_snapshots: bool = True

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        run_id: str | None = None,
        compression: Literal["snappy", "zstd", "none"] = "zstd",
        checkpoint_every_rows: int | None = None,
        accept_events: bool = True,
        accept_metrics: bool = True,
        accept_book_snapshots: bool = True,
    ) -> None:
        # Eagerly import pyarrow so failure surfaces at construction,
        # not at first write.
        try:
            import pyarrow as pa  # noqa: F401
            import pyarrow.parquet as pq  # noqa: F401
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc

        if checkpoint_every_rows is not None and checkpoint_every_rows <= 0:
            raise ValueError(
                f"checkpoint_every_rows must be positive or None, got "
                f"{checkpoint_every_rows!r}"
            )

        self._pa = pa
        self._pq = pq

        self._root: Path = Path(root)
        self._run_id: str = run_id if run_id is not None else "default"
        self._run_dir: Path = self._root / self._run_id
        self._compression: str = compression
        self._checkpoint_every_rows: int | None = checkpoint_every_rows

        # Set per-instance so callers can override after construction.
        self.accept_events = accept_events
        self.accept_metrics = accept_metrics
        self.accept_book_snapshots = accept_book_snapshots

        # Buckets created lazily on first row of each key.
        self._event_buckets: dict[str, _Bucket] = {}
        self._generic_bucket: _Bucket | None = None
        self._metric_buckets: dict[str, _Bucket] = {}
        self._book_buckets: dict[str, _Bucket] = {}

        # Track unknown event types so the warning is emitted only once.
        self._warned_unknown_types: set[str] = set()

        # Column layouts (positional, matching the Arrow schemas).
        # The per-event-type column layout is derived from each schema's
        # fields at first sight of the event_type (see on_event).
        self._generic_event_columns: tuple[str, ...] = (
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "event_type",
            "payload",
            "seq",
        )
        self._metric_columns: tuple[str, ...] = (
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "value",
            "seq",
        )
        self._book_columns: tuple[str, ...] = (
            "sim_time_ns",
            "bids",
            "asks",
            "depth",
            "seq",
        )

        # Per-event-type pickle-position cache, populated lazily.
        self._event_pickle_offsets: dict[str, tuple[int, ...]] = {}

    # ---- EventSink lifecycle ------------------------------------------------

    def on_simulation_start(self, meta: dict) -> None:
        """Reset state and prepare the run directory.

        Re-callable for gym-style restarts: clears buckets, removes
        any existing ``.partial/`` dir, recreates the directory tree.
        """
        self._event_buckets.clear()
        self._generic_bucket = None
        self._metric_buckets.clear()
        self._book_buckets.clear()
        self._warned_unknown_types.clear()
        self._event_pickle_offsets.clear()

        # Wipe any stale partials from a previous run with the same run_id.
        partial_root = self._run_dir / _PARTIAL_DIR
        if partial_root.exists():
            shutil.rmtree(partial_root, ignore_errors=True)

        # Create kind directories upfront so write paths can rely on them.
        for kind in ("events", "metrics", "book_snapshots"):
            (self._run_dir / kind).mkdir(parents=True, exist_ok=True)

    def on_event(self, t: tuple) -> None:
        # Wire tuple: (agent_id, agent_type, sim_time_ns, event_type, payload, seq)
        agent_id, agent_type, sim_time_ns, event_type, payload, seq = t
        schema = EVENT_TYPE_SCHEMA.get(event_type)
        if schema is None:
            self._append_generic_event(
                agent_id, agent_type, sim_time_ns, event_type, payload, seq
            )
            return

        bucket = self._event_buckets.get(event_type)
        if bucket is None:
            bucket = _Bucket(
                _build_event_columns(schema),
                _build_event_schema(self._pa, schema),
            )
            self._event_buckets[event_type] = bucket
            self._event_pickle_offsets[event_type] = _pickle_field_offsets(schema)
        if bucket.next_seq_lo is None:
            bucket.next_seq_lo = seq

        # Normalize payload to a positional tuple matching schema.fields.
        arity = len(schema.fields)
        if arity == 0:
            payload_cells: tuple[Any, ...] = ()
        elif arity == 1:
            payload_cells = (payload,)
        else:
            # ≥ 2 — payload is the positional tuple itself.
            payload_cells = tuple(payload)

        # Pickle any field whose type code is "binary" (DEPTH.levels).
        pickle_offsets = self._event_pickle_offsets[event_type]
        if pickle_offsets:
            payload_list = list(payload_cells)
            for off in pickle_offsets:
                payload_list[off] = _pickle(payload_list[off])
            payload_cells = tuple(payload_list)

        bucket.append((agent_id, agent_type, sim_time_ns, *payload_cells, seq))
        self._maybe_checkpoint(bucket, "events", event_type, seq)

    def _append_generic_event(
        self,
        agent_id: int,
        agent_type: str,
        sim_time_ns: int,
        event_type: str,
        payload: Any,
        seq: int,
    ) -> None:
        if event_type not in self._warned_unknown_types:
            self._warned_unknown_types.add(event_type)
            warnings.warn(
                f"ParquetSink: event_type {event_type!r} is not in "
                "EVENT_TYPE_SCHEMA; routing to '__generic__' bucket.",
                stacklevel=3,
            )
        bucket = self._generic_bucket
        if bucket is None:
            bucket = _Bucket(
                self._generic_event_columns,
                _build_generic_event_schema(self._pa),
            )
            self._generic_bucket = bucket
        if bucket.next_seq_lo is None:
            bucket.next_seq_lo = seq
        bucket.append(
            (agent_id, agent_type, sim_time_ns, event_type, _pickle(payload), seq)
        )
        self._maybe_checkpoint(bucket, "events", _GENERIC_FILENAME, seq)

    def on_metric(self, t: tuple) -> None:
        # Wire tuple: (agent_id, agent_type, sim_time_ns, key, value, seq)
        agent_id, agent_type, sim_time_ns, key, value, seq = t
        bucket = self._metric_buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                self._metric_columns, _build_metric_schema(self._pa, key)
            )
            self._metric_buckets[key] = bucket
        if bucket.next_seq_lo is None:
            bucket.next_seq_lo = seq
        # value is documented as float; coerce to float for Arrow stability.
        bucket.append(
            (agent_id, agent_type, sim_time_ns, float(value), seq)
        )
        self._maybe_checkpoint(bucket, "metrics", key, seq)

    def on_book_snapshot(self, t: tuple) -> None:
        # Wire tuple: (symbol, sim_time_ns, bids, asks, depth, seq)
        symbol, sim_time_ns, bids, asks, depth, seq = t
        bucket = self._book_buckets.get(symbol)
        if bucket is None:
            bucket = _Bucket(
                self._book_columns,
                _build_book_snapshot_schema(self._pa, symbol),
            )
            self._book_buckets[symbol] = bucket
        if bucket.next_seq_lo is None:
            bucket.next_seq_lo = seq
        bucket.append(
            (sim_time_ns, _pickle(bids), _pickle(asks), int(depth), seq)
        )
        self._maybe_checkpoint(bucket, "book_snapshots", symbol, seq)

    def flush(self) -> None:
        return  # No mid-run flushing.

    def on_simulation_end(self, meta: dict) -> None:
        """Flush every non-empty bucket to disk.

        Each file is written first to ``<run_dir>/.partial/<kind>/`` and
        then atomically renamed to its final location via
        :func:`os.replace`.
        """
        for event_type, bucket in self._event_buckets.items():
            if bucket.row_count > 0:
                self._flush_bucket(bucket, "events", event_type, final=True)
        if self._generic_bucket is not None and self._generic_bucket.row_count > 0:
            self._flush_bucket(
                self._generic_bucket, "events", _GENERIC_FILENAME, final=True
            )
        for key, bucket in self._metric_buckets.items():
            if bucket.row_count > 0:
                self._flush_bucket(bucket, "metrics", key, final=True)
        for symbol, bucket in self._book_buckets.items():
            if bucket.row_count > 0:
                self._flush_bucket(bucket, "book_snapshots", symbol, final=True)

        # Best-effort cleanup of empty partial dirs.
        partial_root = self._run_dir / _PARTIAL_DIR
        if partial_root.exists():
            for sub in partial_root.iterdir():
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        logger.warning(
                            "ParquetSink: leftover files in %s; leaving for inspection.",
                            sub,
                        )
            try:
                partial_root.rmdir()
            except OSError:
                pass

    # ---- Internal helpers ---------------------------------------------------

    def _maybe_checkpoint(
        self, bucket: _Bucket, kind: str, key: str, last_seq: int
    ) -> None:
        threshold = self._checkpoint_every_rows
        if threshold is None or bucket.row_count < threshold:
            return
        self._flush_bucket(bucket, kind, key, final=False)

    def _flush_bucket(
        self, bucket: _Bucket, kind: str, key: str, *, final: bool
    ) -> None:
        if bucket.row_count == 0:
            return

        pa_mod = self._pa
        # Build Arrow arrays column-by-column from parallel Python lists.
        # This is faster than per-row builders in current pyarrow versions
        # for the (int, str, binary, float) mix used here.
        arrays = [
            pa_mod.array(col, type=field.type)
            for col, field in zip(bucket.data, bucket.arrow_schema)
        ]
        table = pa_mod.Table.from_arrays(arrays, schema=bucket.arrow_schema)

        # Determine target filename.
        seq_lo = bucket.next_seq_lo if bucket.next_seq_lo is not None else 0
        last_seq = bucket.data[bucket.columns.index("seq")][-1]
        if self._checkpoint_every_rows is None and final and bucket.files_written == 0:
            filename = f"{key}.parquet"
        else:
            filename = f"{key}.{seq_lo}-{last_seq}.parquet"

        dest_dir = self._run_dir / kind
        partial_dir = self._run_dir / _PARTIAL_DIR / kind
        partial_dir.mkdir(parents=True, exist_ok=True)

        partial_path = partial_dir / filename
        final_path = dest_dir / filename

        # Write to .partial/, then atomically rename.  Any failure leaves
        # the partial file for inspection; the reader skips .partial/.
        self._pq.write_table(
            table,
            partial_path,
            compression=_normalize_compression(self._compression),
        )
        os.replace(partial_path, final_path)

        bucket.files_written += 1
        bucket.reset()


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_parquet_logs(
    run_dir: str | os.PathLike[str],
    *,
    kinds: Iterable[Literal["events", "metrics", "book_snapshots"]] = (
        "events",
        "metrics",
        "book_snapshots",
    ),
) -> dict[str, dict[str, pd.DataFrame]]:
    """Read a finished :class:`ParquetSink` run directory.

    Walks ``<run_dir>/<kind>/`` for each requested kind, concatenates
    checkpointed shards in ``seq`` order, and returns one DataFrame
    per key.  Files in ``<run_dir>/.partial/`` are skipped (write was
    in progress at crash time).

    Arguments:
        run_dir:
            The directory created by ``ParquetSink(run_id=...)``
            (i.e. ``<root>/<run_id>``).
        kinds:
            Subset of ``("events", "metrics", "book_snapshots")``
            to read.  Missing kind directories are silently skipped.

    Returns:
        Mapping ``{kind: {key: DataFrame}}``.  For ``"events"`` the
        key is the ``event_type`` (or ``"__generic__"`` for unknown
        types); for ``"metrics"`` the key is the metric name; for
        ``"book_snapshots"`` the key is the symbol.

        Under :data:`BUS_FORMAT_VERSION` ``"2"`` (Phase 2c), per-event
        DataFrames carry one typed column per :class:`PayloadSchema`
        field, so :func:`unpickle_payloads` is needed only for the
        ``__generic__`` bucket (whose ``payload`` column is still
        pickled) and for the ``bids`` / ``asks`` columns in
        ``book_snapshots`` DataFrames.

    Raises:
        ImportError: ``pyarrow`` is not installed.
        ValueError: A file's ``bus_format_version`` metadata does not
            match the running library's :data:`BUS_FORMAT_VERSION`.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc

    run_path = Path(run_dir)
    result: dict[str, dict[str, pd.DataFrame]] = {}

    for kind in kinds:
        kind_dir = run_path / kind
        if not kind_dir.is_dir():
            continue
        # Group files by key (strip optional .<lo>-<hi> shard suffix).
        shards: dict[str, list[Path]] = {}
        for path in sorted(kind_dir.iterdir()):
            if path.is_dir():
                continue
            if path.suffix != ".parquet":
                continue
            key = _strip_shard_suffix(path.stem)
            shards.setdefault(key, []).append(path)

        per_key: dict[str, pd.DataFrame] = {}
        for key, paths in shards.items():
            tables = []
            for p in paths:
                table = pq.read_table(p)
                _validate_file_metadata(table.schema.metadata, p)
                tables.append(table)
            if len(tables) == 1:
                df = tables[0].to_pandas()
            else:
                import pyarrow as pa

                df = pa.concat_tables(tables).to_pandas()
            # Sort by (sim_time_ns, seq) — the canonical bus order.
            sort_cols = [c for c in ("sim_time_ns", "seq") if c in df.columns]
            if sort_cols:
                df = df.sort_values(sort_cols, kind="stable").reset_index(drop=True)
            per_key[key] = df
        if per_key:
            result[kind] = per_key

    return result


def unpickle_payloads(df: pd.DataFrame, column: str = "payload") -> pd.DataFrame:
    """Replace a pickled binary column with its in-memory Python objects.

    Returns a new DataFrame with ``column`` materialized via
    :func:`pickle.loads`.  ``None`` values pass through. If ``column``
    is absent from ``df`` (the typed-Arrow case in
    :data:`BUS_FORMAT_VERSION` ``"2"``), the input is returned
    unchanged.
    """
    if column not in df.columns:
        return df
    out = df.copy()
    out[column] = out[column].map(lambda b: pickle.loads(b) if b is not None else None)  # noqa: S301
    return out


# ---------------------------------------------------------------------------
# Module-internal helpers
# ---------------------------------------------------------------------------


def _pickle(obj: Any) -> bytes:
    return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


def _normalize_compression(c: str) -> str | None:
    return None if c == "none" else c


def _strip_shard_suffix(stem: str) -> str:
    """Strip a trailing ``.<lo>-<hi>`` shard suffix from a file stem.

    ``"FOO.0-99"`` → ``"FOO"``; ``"FOO"`` → ``"FOO"``; ``"foo.bar.0-9"``
    → ``"foo.bar"`` (keeps interior dots).
    """
    last_dot = stem.rfind(".")
    if last_dot == -1:
        return stem
    suffix = stem[last_dot + 1 :]
    if "-" not in suffix:
        return stem
    lo, _, hi = suffix.partition("-")
    if lo.isdigit() and hi.isdigit():
        return stem[:last_dot]
    return stem


def _validate_file_metadata(
    md: dict[bytes, bytes] | None, path: Path
) -> None:
    """Raise on bus-format-version mismatch."""
    if md is None:
        return
    key = f"{_META_PREFIX}bus_format_version".encode()
    raw = md.get(key)
    if raw is None:
        return
    version = raw.decode("ascii")
    if version != BUS_FORMAT_VERSION:
        raise ValueError(
            f"ParquetSink file {path} was written with "
            f"bus_format_version={version!r}, but this library expects "
            f"{BUS_FORMAT_VERSION!r}. The on-disk format is incompatible."
        )

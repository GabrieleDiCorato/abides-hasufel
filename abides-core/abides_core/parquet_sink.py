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

Payload encoding (Phase 3 MVP)
------------------------------

Event payloads in Phase 2/3 are heterogeneous Python objects (dicts,
strings, scalars, lists), so this sink stores them as ``pickle``
binary in a single ``payload`` column.  The schema name and version
from :mod:`~abides_core.event_payloads` are recorded in the Parquet
file's key/value metadata for round-trip validation.

When event payloads are normalized to typed tuples (Phase 2a), this
sink can be extended with per-schema typed columns without breaking
the on-disk layout for the existing pickled-payload column — the
metadata version bump signals the change to readers.

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

BUS_FORMAT_VERSION = "1"
"""Versions the on-disk layout/contract.  Bump on incompatible changes."""

_META_PREFIX = "abides."
_PARTIAL_DIR = ".partial"
_GENERIC_FILENAME = "__generic__"
_INSTALL_HINT = (
    "pyarrow is required to use ParquetSink. Install it with: "
    "pip install 'abides-ng[parquet]'"
)


# ---------------------------------------------------------------------------
# Arrow schema builders (lazy, built after pyarrow import succeeds)
# ---------------------------------------------------------------------------


def _build_event_schema(pa_mod: Any, schema: PayloadSchema) -> pa.Schema:
    """Arrow schema for a per-event-type file.

    Phase 3 MVP: ``payload`` is pickled binary; ``schema_name`` /
    ``schema_version`` recorded as Parquet file metadata.
    """
    return pa_mod.schema(
        [
            pa_mod.field("agent_id", pa_mod.int64(), nullable=False),
            pa_mod.field("agent_type", pa_mod.string(), nullable=False),
            pa_mod.field("sim_time_ns", pa_mod.int64(), nullable=False),
            pa_mod.field("payload", pa_mod.binary(), nullable=True),
            pa_mod.field("seq", pa_mod.int64(), nullable=False),
        ],
        metadata={
            f"{_META_PREFIX}bus_format_version": BUS_FORMAT_VERSION,
            f"{_META_PREFIX}schema_name": schema.name,
            f"{_META_PREFIX}schema_version": str(schema.version),
        },
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
        self._event_columns: tuple[str, ...] = (
            "agent_id",
            "agent_type",
            "sim_time_ns",
            "payload",
            "seq",
        )
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
                self._event_columns, _build_event_schema(self._pa, schema)
            )
            self._event_buckets[event_type] = bucket
        if bucket.next_seq_lo is None:
            bucket.next_seq_lo = seq
        bucket.append((agent_id, agent_type, sim_time_ns, _pickle(payload), seq))
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

        Events DataFrames carry pickled payloads in the ``payload``
        column.  Call :func:`unpickle_payloads` to materialize them.

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
    :func:`pickle.loads`.  ``None`` values pass through.
    """
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

"""Event sink Protocol and shipped concrete sinks.

Every :class:`EventSink` receives bus-dispatched wire tuples from the
:class:`~abides_core.event_bus.EventBus`.  The bus calls the sink lifecycle
hooks at simulation boundaries and dispatches event/metric/book-snapshot
tuples during the run.

**Wire tuple shapes** (see :mod:`~abides_core.event_records` for field docs):

.. code-block:: text

    event:         (agent_id, agent_type, sim_time_ns, event_type, payload, seq)
    metric:        (agent_id, agent_type, sim_time_ns, key, value, seq)
    book_snapshot: (symbol, sim_time_ns, bids, asks, depth, seq)

**Shipped sinks:**

* :class:`InMemorySink` — accumulates all events in memory.  Provides
  :meth:`~InMemorySink.agent_log` for backward-compatible ``Agent.log``
  reconstruction and :meth:`~InMemorySink.to_dataframe` for analysis.
* :class:`BZ2PickleSink` — writes per-agent bzip2-pickled DataFrames at
  simulation end (identical format to the legacy ``kernel_terminating()``
  path).
* :class:`MetricsObserverSink` — routes metric tuples to a chain of
  :class:`~abides_core.observers.KernelObserver` instances.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd

from .event_payloads import EVENT_TYPE_SCHEMA, GENERIC, PayloadSchema
from .event_records import WIRE_FIELDS_EVENT, NanosecondTime
from .log_writer import LogWriter
from .observers import KernelObserver

if TYPE_CHECKING:
    from .agent import Agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EventSink Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EventSink(Protocol):
    """Minimal contract any event sink must satisfy.

    Implementations declare which tuple kinds they care about via the
    ``accept_*`` attributes.  The bus reads these **once at start time**
    and skips the call entirely for disabled kinds, so no-op returns
    are never called.  Setting ``accept_events = False`` is therefore
    more efficient than overriding :meth:`on_event` with a ``return``
    body.

    All methods are called **synchronously** from the kernel runner loop
    via :meth:`~abides_core.event_bus.EventBus.drain`.  Implementations
    must not block or raise under normal conditions.  Exceptions are
    caught by the bus, the sink is marked failed, and the exception is
    surfaced at :meth:`~abides_core.event_bus.EventBus.shutdown`.
    """

    accept_events: bool
    accept_metrics: bool
    accept_book_snapshots: bool

    def on_simulation_start(self, meta: dict) -> None:
        """Called once after all sinks are registered, before the first
        :meth:`~abides_core.event_bus.EventBus.drain`.

        ``meta`` carries context from the kernel (e.g. ``sim_id``).
        """

    def on_event(self, t: tuple) -> None:
        """Receive one event wire tuple.

        Only called when ``accept_events is True``.
        """

    def on_metric(self, t: tuple) -> None:
        """Receive one metric wire tuple.

        Only called when ``accept_metrics is True``.
        """

    def on_book_snapshot(self, t: tuple) -> None:
        """Receive one book-snapshot wire tuple.

        Only called when ``accept_book_snapshots is True``.
        """

    def flush(self) -> None:
        """Optional eager flush (e.g. to disk mid-simulation).

        The bus calls this when the kernel explicitly requests a flush.
        The default no-op is acceptable for in-memory sinks.
        """

    def on_simulation_end(self, meta: dict) -> None:
        """Called once after the last :meth:`~abides_core.event_bus.EventBus.drain`,
        before the kernel tears down.

        ``meta`` carries the same context as :meth:`on_simulation_start`.
        Disk-writing sinks should commit all buffered data here.
        """


# ---------------------------------------------------------------------------
# InMemorySink
# ---------------------------------------------------------------------------


class InMemorySink:
    """Accumulates all events in memory using a schema-driven columnar layout.

    Internal storage is :attr:`_cols`, a ``dict[event_type, dict[column,
    list]]`` keyed by event type. Each bucket carries the four common
    wire columns (``agent_id``, ``agent_type``, ``sim_time_ns``,
    ``seq``) plus one column per field declared by the matching
    :class:`~abides_core.event_payloads.PayloadSchema`. Event types
    absent from :data:`~abides_core.event_payloads.EVENT_TYPE_SCHEMA`
    fall back to a single ``payload`` column under the
    :data:`~abides_core.event_payloads.GENERIC` schema, and a one-time
    warning is logged so we know the registry is incomplete.

    Provides:
    * :meth:`agent_log` — backward-compatible ``(time, event_type,
      payload)`` list for one agent, identical shape to the old
      ``Agent.log`` list.
    * :meth:`to_dataframe` — full wide-flat event table with columns
      from :data:`~abides_core.event_records.WIRE_FIELDS_EVENT`,
      reconstructed from the columnar buckets and sorted by ``seq``.
    * :attr:`events` — wire tuples reconstructed on demand in
      publication (``seq``) order. Backward-compatible with code that
      iterates ``sink.events`` expecting ``(agent_id, agent_type,
      sim_time_ns, event_type, payload, seq)``.

    Metrics and book snapshots are captured if ``accept_metrics`` /
    ``accept_book_snapshots`` are left ``True`` (the defaults).
    """

    accept_events: bool = True
    accept_metrics: bool = True
    accept_book_snapshots: bool = True

    # Common columns present in every bucket, in WIRE_FIELDS_EVENT order
    # minus event_type (the bucket key) and payload (replaced by schema
    # fields or a single ``payload`` column for GENERIC).
    _COMMON_COLS: tuple[str, ...] = ("agent_id", "agent_type", "sim_time_ns", "seq")

    def __init__(self) -> None:
        self._cols: dict[str, dict[str, list]] = {}
        self._metrics: list[tuple] = []
        self._book_snapshots: list[tuple] = []
        # Schema selected per event_type (one entry per bucket key);
        # cached so we don't re-do the dict lookup on every event.
        self._bucket_schema: dict[str, PayloadSchema] = {}
        # Event types we have already warned about for GENERIC fallback.
        self._warned_generic: set[str] = set()
        self._event_count: int = 0

    # ---- EventSink lifecycle --------------------------------------------------

    def on_simulation_start(self, meta: dict) -> None:
        self._cols.clear()
        self._bucket_schema.clear()
        self._warned_generic.clear()
        self._metrics.clear()
        self._book_snapshots.clear()
        self._event_count = 0

    def on_event(self, t: tuple) -> None:
        # Wire tuple shape: (agent_id, agent_type, sim_time_ns, event_type, payload, seq)
        agent_id, agent_type, sim_time_ns, event_type, payload, seq = t

        schema = self._bucket_schema.get(event_type)
        if schema is None:
            schema = EVENT_TYPE_SCHEMA.get(event_type, GENERIC)
            self._bucket_schema[event_type] = schema
            if schema is GENERIC and event_type not in self._warned_generic:
                self._warned_generic.add(event_type)
                logger.warning(
                    "InMemorySink: event_type %r is not registered in "
                    "EVENT_TYPE_SCHEMA; falling back to GENERIC bucket.",
                    event_type,
                )

        bucket = self._cols.get(event_type)
        if bucket is None:
            bucket = {col: [] for col in self._COMMON_COLS}
            if schema is GENERIC:
                bucket["payload"] = []
            else:
                for field in schema.fields:
                    bucket[field] = []
            self._cols[event_type] = bucket

        # Decompose the payload against the schema before mutating any
        # column so the append is transactional: if the payload shape
        # does not match the schema we route the row to the GENERIC
        # bucket instead of leaving the typed bucket in a torn state.
        if schema is GENERIC:
            field_values: tuple = (payload,)
            field_names: tuple[str, ...] = ("payload",)
        else:
            arity = len(schema.fields)
            if arity == 0:
                field_values = ()
                field_names = ()
            elif arity == 1:
                # Arity-1 payloads are stored as the bare scalar; if the
                # caller wrapped it in a 1-tuple we unwrap defensively.
                if isinstance(payload, tuple) and len(payload) == 1:
                    field_values = (payload[0],)
                else:
                    field_values = (payload,)
                field_names = schema.fields
            else:
                # Arity ≥ 2 must be a positional tuple of matching length.
                if isinstance(payload, tuple) and len(payload) == arity:
                    field_values = payload
                    field_names = schema.fields
                else:
                    # Shape mismatch: divert to the GENERIC bucket so we
                    # never silently drop a row or torn-write a column.
                    self._fallback_to_generic(
                        event_type, agent_id, agent_type, sim_time_ns, payload, seq
                    )
                    return

        # Atomic append: common columns first, then schema fields.
        bucket["agent_id"].append(agent_id)
        bucket["agent_type"].append(agent_type)
        bucket["sim_time_ns"].append(sim_time_ns)
        bucket["seq"].append(seq)
        for name, value in zip(field_names, field_values, strict=True):
            bucket[name].append(value)
        self._event_count += 1

    def _fallback_to_generic(
        self,
        event_type: str,
        agent_id: int,
        agent_type: str,
        sim_time_ns: NanosecondTime,
        payload: Any,
        seq: int,
    ) -> None:
        """Route a schema-mismatched row to a dedicated GENERIC bucket.

        The bucket key is suffixed with ``"::generic"`` so the typed
        bucket (if any) retains its uniform column shapes.  A single
        warning per event_type is emitted to flag the producer.
        """
        if event_type not in self._warned_generic:
            self._warned_generic.add(event_type)
            logger.warning(
                "InMemorySink: payload for event_type %r does not match its "
                "schema; routing to GENERIC fallback bucket.",
                event_type,
            )
        fallback_key = f"{event_type}::generic"
        bucket = self._cols.get(fallback_key)
        if bucket is None:
            bucket = {col: [] for col in self._COMMON_COLS}
            bucket["payload"] = []
            self._cols[fallback_key] = bucket
            self._bucket_schema[fallback_key] = GENERIC
        bucket["agent_id"].append(agent_id)
        bucket["agent_type"].append(agent_type)
        bucket["sim_time_ns"].append(sim_time_ns)
        bucket["seq"].append(seq)
        bucket["payload"].append(payload)
        self._event_count += 1

    def on_metric(self, t: tuple) -> None:
        self._metrics.append(t)

    def on_book_snapshot(self, t: tuple) -> None:
        self._book_snapshots.append(t)

    def flush(self) -> None:
        return  # no-op for in-memory

    def on_simulation_end(self, meta: dict) -> None:
        return  # no-op; data stays in memory

    # ---- Reconstruction helpers ---------------------------------------------

    def _reconstruct_payload(self, bucket_key: str, idx: int) -> Any:
        """Reassemble the original payload for row ``idx`` of ``bucket_key``."""
        bucket = self._cols[bucket_key]
        schema = self._bucket_schema[bucket_key]
        if schema is GENERIC:
            return bucket["payload"][idx]
        arity = len(schema.fields)
        if arity == 0:
            return ()
        if arity == 1:
            return bucket[schema.fields[0]][idx]
        return tuple(bucket[name][idx] for name in schema.fields)

    def _iter_rows(self) -> list[tuple]:
        """Reconstruct all rows as wire tuples, sorted by ``seq``."""
        rows: list[tuple] = []
        for bucket_key, bucket in self._cols.items():
            # The schema-mismatch fallback uses a ``"<type>::generic"``
            # bucket key; the surfaced event_type strips that suffix so
            # readers see the original type.
            event_type = (
                bucket_key[: -len("::generic")]
                if bucket_key.endswith("::generic")
                else bucket_key
            )
            n = len(bucket["seq"])
            for i in range(n):
                rows.append(
                    (
                        bucket["agent_id"][i],
                        bucket["agent_type"][i],
                        bucket["sim_time_ns"][i],
                        event_type,
                        self._reconstruct_payload(bucket_key, i),
                        bucket["seq"][i],
                    )
                )
        rows.sort(key=lambda r: r[5])
        return rows

    # ---- Public API ----------------------------------------------------------

    @property
    def events(self) -> list[tuple]:
        """Wire-format event tuples in publication (``seq``) order.

        Reconstructed on each call from the columnar buckets; if you
        plan to scan more than once, cache the returned list.

        Shape: ``(agent_id, agent_type, sim_time_ns, event_type,
        payload, seq)`` — see :data:`~abides_core.event_records.WIRE_FIELDS_EVENT`.
        """
        return self._iter_rows()

    @property
    def metrics(self) -> list[tuple]:
        """Raw metric wire tuples in publication order."""
        return self._metrics

    @property
    def book_snapshots(self) -> list[tuple]:
        """Raw book-snapshot wire tuples in publication order."""
        return self._book_snapshots

    @property
    def columns(self) -> dict[str, dict[str, list]]:
        """Direct read-only access to the columnar bucket storage.

        Keys are event_type strings; values are ``dict[column_name,
        list]``. Common columns are ``agent_id``, ``agent_type``,
        ``sim_time_ns``, ``seq``; the remaining columns mirror the
        :class:`~abides_core.event_payloads.PayloadSchema` fields for
        the matching event_type, or a single ``payload`` column for
        events that fell through to the
        :data:`~abides_core.event_payloads.GENERIC` schema.

        Do not mutate the returned dicts; intended for zero-copy
        analytics paths such as Arrow exporters.
        """
        return self._cols

    def bucket_schema(self, event_type: str) -> PayloadSchema | None:
        """Return the :class:`PayloadSchema` chosen for ``event_type``.

        ``None`` if no events of that type have been seen yet.
        """
        return self._bucket_schema.get(event_type)

    def agent_log(self, agent_id: int) -> list[tuple[NanosecondTime, str, Any]]:
        """Return ``(sim_time_ns, event_type, payload)`` triples for one agent.

        This reproduces the shape of the old ``Agent.log`` list so
        code that reads ``agent.log`` continues to work after the
        deprecation shim delegates here.

        Events are returned in publication order (monotonically
        increasing ``seq``).

        Arguments:
            agent_id: The integer agent identifier.

        Returns:
            A new list of ``(sim_time_ns, event_type, payload)`` tuples
            for the requested agent.  Empty if the agent produced no events.
        """
        out: list[tuple[NanosecondTime, str, Any]] = []
        for bucket_key, bucket in self._cols.items():
            event_type = (
                bucket_key[: -len("::generic")]
                if bucket_key.endswith("::generic")
                else bucket_key
            )
            agent_ids = bucket["agent_id"]
            times = bucket["sim_time_ns"]
            seqs = bucket["seq"]
            for i, aid in enumerate(agent_ids):
                if aid == agent_id:
                    out.append(
                        (
                            times[i],
                            event_type,
                            self._reconstruct_payload(bucket_key, i),
                            seqs[i],
                        )
                    )
        out.sort(key=lambda r: r[3])
        return [(t, et, p) for t, et, p, _seq in out]

    def to_dataframe(self) -> pd.DataFrame:
        """Return all events as a wide-flat :class:`pandas.DataFrame`.

        Columns match :data:`~abides_core.event_records.WIRE_FIELDS_EVENT`.
        The DataFrame is materialised by reconstructing wire tuples
        from the columnar storage; mutating the returned frame does
        not affect the sink's internal state.

        Returns:
            An empty DataFrame (with correct columns) when no events have
            been captured.
        """
        if self._event_count == 0:
            return pd.DataFrame(columns=list(WIRE_FIELDS_EVENT))
        return pd.DataFrame(self._iter_rows(), columns=list(WIRE_FIELDS_EVENT))


# ---------------------------------------------------------------------------
# BZ2PickleSink
# ---------------------------------------------------------------------------


class BZ2PickleSink:
    """Writes per-agent bzip2-pickled DataFrames at simulation end.

    The on-disk format is **byte-for-byte identical** to the legacy
    ``kernel_terminating()`` path:

    .. code-block:: python

        pd.DataFrame(
            [(t[2], t[3], t[4]) for t in agent_events],
            columns=["EventTime", "EventType", "Event"],
        ).set_index("EventTime")

    This ensures ``parse_logs_df()`` and downstream analysis notebooks
    continue to work without modification.

    .. deprecated::
        Slated for removal in a future legacy-logging cleanup. Replace with
        :class:`abides_core.parquet_sink.ParquetSink` (columnar,
        crash-safe, streaming) or another EventBus sink.

    Arguments:
        log_writer: A :class:`~abides_core.log_writer.LogWriter` instance.
            The kernel passes its own ``_log_writer`` here.
        agents:     The full list of :class:`~abides_core.agent.Agent`
            instances.  Used to map ``agent_id \u2192 agent_name`` for the
            file naming convention.
    """

    accept_events: bool = True
    accept_metrics: bool = False
    accept_book_snapshots: bool = False

    # One-shot DeprecationWarning per process. Gym episode loops and
    # parameter sweeps must not flood stderr with duplicates.
    _deprecation_warned: bool = False

    def __init__(
        self,
        log_writer: LogWriter,
        agents: Sequence[Agent],
    ) -> None:
        if not BZ2PickleSink._deprecation_warned:
            BZ2PickleSink._deprecation_warned = True
            warnings.warn(
                "BZ2PickleSink is deprecated and will be removed in a "
                "future release; migrate to ParquetSink "
                "(``abides_core.parquet_sink.ParquetSink``) or another "
                "EventBus-registered columnar sink.",
                DeprecationWarning,
                stacklevel=2,
            )
        self._log_writer = log_writer
        # Retain the agents reference; ``log_to_file`` is read at
        # on_simulation_start() time so post-construction changes to the
        # flag are honoured (the legacy kernel_terminating() path read
        # the flag lazily too).
        self._agents: Sequence[Agent] = agents
        self._id_to_name: dict[int, str] = {}
        self._write_to_file_ids: frozenset[int] = frozenset()
        self._by_agent: dict[int, list[tuple]] = {}

    # ---- EventSink lifecycle --------------------------------------------------

    def on_simulation_start(self, meta: dict) -> None:
        # Snapshot the agent name and log_to_file flag at start() time so
        # any mutation between Kernel.__init__() and Kernel.initialize()
        # is honoured.  Each restart (gym reset) re-reads the current
        # values.
        self._id_to_name = {a.id: a.name for a in self._agents}
        self._write_to_file_ids = frozenset(a.id for a in self._agents if a.log_to_file)
        self._by_agent.clear()

    def on_event(self, t: tuple) -> None:
        agent_id: int = t[0]
        if agent_id not in self._write_to_file_ids:
            return
        bucket = self._by_agent.get(agent_id)
        if bucket is None:
            bucket = []
            self._by_agent[agent_id] = bucket
        bucket.append(t)

    def on_metric(self, t: tuple) -> None:  # pragma: no cover — accept_metrics=False
        return

    def on_book_snapshot(self, t: tuple) -> None:  # pragma: no cover
        return

    def flush(self) -> None:
        return  # writes are batch-only at simulation end

    def on_simulation_end(self, meta: dict) -> None:
        """Write one ``.bz2`` file per agent that produced events."""
        for agent_id, events in self._by_agent.items():
            agent_name = self._id_to_name.get(agent_id, f"agent_{agent_id}")
            df = pd.DataFrame(
                [(t[2], t[3], t[4]) for t in events],
                columns=["EventTime", "EventType", "Event"],
            ).set_index("EventTime")
            try:
                self._log_writer.write_agent_log(agent_name, df)
            except Exception:
                logger.exception(
                    "BZ2PickleSink failed to write log for agent %r (id=%d)",
                    agent_name,
                    agent_id,
                )


# ---------------------------------------------------------------------------
# MetricsObserverSink
# ---------------------------------------------------------------------------


class MetricsObserverSink:
    """Routes metric tuples to a chain of :class:`~abides_core.observers.KernelObserver` instances.

    This sink accepts **metrics only** (``accept_events = False``).  The
    bus skips :meth:`on_event` calls entirely for efficiency.

    The existing ``KernelObserver.on_terminate(kernel)`` lifecycle hook is
    **not** moved to the bus.  It stays on ``Kernel.terminate()`` to
    preserve backward compatibility.  This sink only handles the
    per-metric dispatch that used to live in ``Agent.report_metric()``.

    Arguments:
        observers: Zero or more :class:`~abides_core.observers.KernelObserver`
            instances to forward metrics to.
    """

    accept_events: bool = False
    accept_metrics: bool = True
    accept_book_snapshots: bool = False

    def __init__(self, observers: Sequence[KernelObserver]) -> None:
        self._observers: tuple[KernelObserver, ...] = tuple(observers)

    # ---- EventSink lifecycle --------------------------------------------------

    def on_simulation_start(self, meta: dict) -> None:
        return

    def on_event(self, t: tuple) -> None:  # pragma: no cover — accept_events=False
        return

    def on_metric(self, t: tuple) -> None:
        # Wire layout: (agent_id, agent_type, sim_time_ns, key, value, seq)
        agent_id: int = t[0]
        agent_type: str = t[1]
        key: str = t[3]
        value: float = t[4]
        for obs in self._observers:
            obs.on_metric(agent_id, agent_type, key, value)

    def on_book_snapshot(self, t: tuple) -> None:  # pragma: no cover
        return

    def flush(self) -> None:
        return

    def on_simulation_end(self, meta: dict) -> None:
        return


# ---------------------------------------------------------------------------
# Order-book sinks
# ---------------------------------------------------------------------------


class OrderBookSnapshotMemorySink:
    """In-memory collector for per-symbol book-snapshot tuples.

    Filters incoming ``(symbol, sim_time_ns, bids, asks, depth, seq)``
    tuples by ``symbol`` and stores parallel columnar arrays.  Exposes
    :meth:`as_book_log2` to materialize the legacy ``OrderBook.book_log2``
    shape (tuple of ``{"QuoteTime", "bids", "asks"}`` dicts) for backward
    compatibility with extractors that still expect that format.

    Arguments:
        symbol: Symbol this sink listens for.  Snapshots for other
            symbols are dropped.
        depth: Documented capture depth (not enforced; the publisher
            controls how many levels are sent).
    """

    accept_events: bool = False
    accept_metrics: bool = False
    accept_book_snapshots: bool = True

    def __init__(self, symbol: str, depth: int) -> None:
        self.symbol: str = symbol
        self.depth: int = depth
        self._times: list[NanosecondTime] = []
        self._bids: list[Sequence[tuple[int, int]]] = []
        self._asks: list[Sequence[tuple[int, int]]] = []

    def on_simulation_start(self, meta: dict) -> None:
        return

    def on_event(
        self, t: tuple
    ) -> None:  # pragma: no cover — disabled by accept_events
        return

    def on_metric(
        self, t: tuple
    ) -> None:  # pragma: no cover — disabled by accept_metrics
        return

    def on_book_snapshot(self, t: tuple) -> None:
        symbol, sim_time_ns, bids, asks, _depth, _seq = t
        if symbol != self.symbol:
            return
        self._times.append(sim_time_ns)
        self._bids.append(bids)
        self._asks.append(asks)

    def flush(self) -> None:
        return

    def on_simulation_end(self, meta: dict) -> None:
        return

    def __len__(self) -> int:
        return len(self._times)

    def as_book_log2(self) -> tuple[dict[str, Any], ...]:
        """Materialize the legacy ``OrderBook.book_log2`` shape.

        Returns a tuple of ``{"QuoteTime", "bids", "asks"}`` dicts where
        ``bids`` and ``asks`` are :class:`numpy.ndarray` instances
        (matching the historical type).  The returned tuple is a fresh
        allocation; mutating it does not affect sink state.
        """
        import numpy as np

        return tuple(
            {"QuoteTime": t, "bids": np.array(b), "asks": np.array(a)}
            for t, b, a in zip(self._times, self._bids, self._asks, strict=True)
        )


class OrderBookHistoryMemorySink:
    """In-memory collector for per-symbol order-book event history.

    Filters incoming event tuples by ``event_type`` (against a fixed
    allowlist) and by ``payload.symbol`` (book event payloads carry
    ``symbol`` as a NamedTuple field so multi-symbol exchanges can be
    demultiplexed).  Stores the raw wire tuples for cheap iteration.

    Arguments:
        symbol: Symbol this sink listens for.  Events whose payload
            ``.symbol`` does not match are dropped.
        event_types: Allowlist of event-type strings (e.g.
            ``frozenset({"LIMIT", "EXEC", "CANCEL"})``).  Events of other
            types are dropped at filter time.
    """

    accept_events: bool = True
    accept_metrics: bool = False
    accept_book_snapshots: bool = False

    def __init__(self, symbol: str, event_types: frozenset[str]) -> None:
        self.symbol: str = symbol
        self.event_types: frozenset[str] = event_types
        self._tuples: list[tuple] = []

    def on_simulation_start(self, meta: dict) -> None:
        return

    def on_event(self, t: tuple) -> None:
        _agent_id, _agent_type, _sim_time_ns, event_type, payload, _seq = t
        if event_type not in self.event_types:
            return
        if getattr(payload, "symbol", None) != self.symbol:
            return
        self._tuples.append(t)

    def on_metric(
        self, t: tuple
    ) -> None:  # pragma: no cover — disabled by accept_metrics
        return

    def on_book_snapshot(
        self, t: tuple
    ) -> None:  # pragma: no cover — disabled by accept_book_snapshots
        return

    def flush(self) -> None:
        return

    def on_simulation_end(self, meta: dict) -> None:
        return

    def __len__(self) -> int:
        return len(self._tuples)

    def entries(self) -> tuple[tuple, ...]:
        """Return all captured wire tuples (filtered by symbol + event types)."""
        return tuple(self._tuples)

    def as_history_dicts(self) -> tuple[dict[str, Any], ...]:
        """Materialize the legacy ``OrderBook.history`` shape.

        Each entry is a dict with ``time`` and ``type`` keys followed by
        the payload's NamedTuple fields (``symbol`` is dropped to match
        the historical shape exactly).  The returned tuple is a fresh
        allocation.
        """
        out: list[dict[str, Any]] = []
        for _aid, _atype, sim_time_ns, event_type, payload, _seq in self._tuples:
            d: dict[str, Any] = {"time": sim_time_ns, "type": event_type}
            payload_dict = (
                payload._asdict() if hasattr(payload, "_asdict") else dict(payload)
            )
            payload_dict.pop("symbol", None)
            d.update(payload_dict)
            out.append(d)
        return tuple(out)


# ---------------------------------------------------------------------------
# Deprecation helper used by the Agent.log shim
# ---------------------------------------------------------------------------


def _warn_agent_log_deprecated() -> None:
    """Emit a :class:`DeprecationWarning` for ``Agent.log`` access.

    Called from the ``Agent.log`` property shim.  Uses ``stacklevel=4``
    so the warning points at the *caller's* code, not at the shim itself.
    """
    warnings.warn(
        "Agent.log is deprecated and will be removed in a future version. "
        "Use kernel.event_bus.in_memory_sink.agent_log(agent_id) or "
        "SimulationResult.logs instead.",
        DeprecationWarning,
        stacklevel=4,
    )

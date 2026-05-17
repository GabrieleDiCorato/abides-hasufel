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
    """Accumulates all events in memory.

    Provides:
    * :meth:`agent_log` — backward-compatible ``(time, event_type,
      payload)`` list for one agent, identical shape to the old
      ``Agent.log`` list.
    * :meth:`to_dataframe` — full event table with columns from
      :data:`~abides_core.event_records.WIRE_FIELDS_EVENT`.
    * :attr:`events` — raw wire tuples for zero-copy access.

    Metrics and book snapshots are captured if ``accept_metrics`` /
    ``accept_book_snapshots`` are left ``True`` (the defaults).
    """

    accept_events: bool = True
    accept_metrics: bool = True
    accept_book_snapshots: bool = True

    def __init__(self) -> None:
        self._events: list[tuple] = []
        self._metrics: list[tuple] = []
        self._book_snapshots: list[tuple] = []

    # ---- EventSink lifecycle --------------------------------------------------

    def on_simulation_start(self, meta: dict) -> None:
        self._events.clear()
        self._metrics.clear()
        self._book_snapshots.clear()

    def on_event(self, t: tuple) -> None:
        self._events.append(t)

    def on_metric(self, t: tuple) -> None:
        self._metrics.append(t)

    def on_book_snapshot(self, t: tuple) -> None:
        self._book_snapshots.append(t)

    def flush(self) -> None:
        return  # no-op for in-memory

    def on_simulation_end(self, meta: dict) -> None:
        return  # no-op; data stays in memory

    # ---- Public API ----------------------------------------------------------

    @property
    def events(self) -> list[tuple]:
        """Raw event wire tuples in publication order.

        Shape: ``(agent_id, agent_type, sim_time_ns, event_type,
        payload, seq)`` — see :data:`~abides_core.event_records.WIRE_FIELDS_EVENT`.
        """
        return self._events

    @property
    def metrics(self) -> list[tuple]:
        """Raw metric wire tuples in publication order."""
        return self._metrics

    @property
    def book_snapshots(self) -> list[tuple]:
        """Raw book-snapshot wire tuples in publication order."""
        return self._book_snapshots

    def agent_log(self, agent_id: int) -> list[tuple[NanosecondTime, str, Any]]:
        """Return ``(sim_time_ns, event_type, payload)`` triples for one agent.

        This reproduces the shape of the old ``Agent.log`` list so
        code that reads ``agent.log`` continues to work after the
        deprecation shim delegates here.

        Events are returned in publication order (monotonically
        increasing ``seq``).  If order matters, callers should sort
        by ``sim_time_ns`` after the fact.

        Arguments:
            agent_id: The integer agent identifier.

        Returns:
            A new list of ``(sim_time_ns, event_type, payload)`` tuples
            for the requested agent.  Empty if the agent produced no events.
        """
        return [(t[2], t[3], t[4]) for t in self._events if t[0] == agent_id]

    def to_dataframe(self) -> pd.DataFrame:
        """Return all events as a :class:`pandas.DataFrame`.

        Columns match :data:`~abides_core.event_records.WIRE_FIELDS_EVENT`.
        The DataFrame is a fresh allocation; mutating it does not affect
        the sink's internal state.

        Returns:
            An empty DataFrame (with correct columns) when no events have
            been captured.
        """
        if not self._events:
            return pd.DataFrame(columns=list(WIRE_FIELDS_EVENT))
        return pd.DataFrame(self._events, columns=list(WIRE_FIELDS_EVENT))


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
        Slated for removal in the Phase 5+2 cleanup of the event-
        logging refactor. Replace with
        :class:`abides_core.parquet_sink.ParquetSink` (columnar,
        crash-safe, streaming) or another EventBus sink. See
        ``docs/active-plans/event-logging-refactor-plan.md`` \u00a75 for
        the deprecation timeline.

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
                "EventBus-registered columnar sink. See "
                "docs/active-plans/event-logging-refactor-plan.md \u00a75.",
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

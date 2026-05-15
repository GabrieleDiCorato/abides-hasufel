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

    Arguments:
        log_writer: A :class:`~abides_core.log_writer.LogWriter` instance.
            The kernel passes its own ``_log_writer`` here.
        agents:     The full list of :class:`~abides_core.agent.Agent`
            instances.  Used to map ``agent_id → agent_name`` for the
            file naming convention.
    """

    accept_events: bool = True
    accept_metrics: bool = False
    accept_book_snapshots: bool = False

    def __init__(
        self,
        log_writer: LogWriter,
        agents: Sequence[Agent],
    ) -> None:
        self._log_writer = log_writer
        self._id_to_name: dict[int, str] = {a.id: a.name for a in agents}
        # Honour the per-agent log_to_file flag.  Agents with log_to_file=False
        # publish events to InMemorySink (if registered) but not to disk.
        self._write_to_file_ids: frozenset[int] = frozenset(
            a.id for a in agents if a.log_to_file
        )
        self._by_agent: dict[int, list[tuple]] = {}

    # ---- EventSink lifecycle --------------------------------------------------

    def on_simulation_start(self, meta: dict) -> None:
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

"""Single-threaded event bus for ABIDES.

The bus is the **central dispatch point** for all telemetry emitted
during a simulation.  It is owned by :class:`~abides_core.kernel.Kernel`,
created once per simulation, and threaded through the simulation lifecycle:

.. code-block:: text

    Kernel.initialize()
        └── EventBus.start(meta)
                └── sink.on_simulation_start(meta) for each sink

    Kernel.runner() — per handler:
        └── EventBus.drain()
                └── sink.on_event / on_metric / on_book_snapshot(t)
                      for each buffered tuple

    Kernel.terminate()
        └── EventBus.shutdown(meta)
                └── EventBus.drain()          ← final flush
                └── sink.on_simulation_end(meta) for each non-failed sink

**Hot-path design:**
Publishers call ``bus.publish_event(...)`` *after* returning from a
handler; the bus appends to an in-memory list and returns.  The actual
dispatch to sinks happens in ``drain()``, called once per handler
invocation.  No sink I/O occurs on the critical agent event path.

**No-op rebinding:**
At ``start()`` time the bus inspects which sinks accept each kind.  If no
sink accepts events, ``publish_event`` is rebound to a module-level
zero-body function so subsequent calls are pure Python function-call
overhead with no attribute lookups.

**Sink failure isolation:**
Exceptions raised in sink callbacks are caught per-sink.  The failed sink
is removed from the active dispatch set, and the error is recorded.  At
``shutdown()`` time all recorded failures are re-raised as a single
:class:`RuntimeError` so the kernel can surface them after teardown.

**Gym / multiple starts:**
``start()`` clears all ring buffers and resets the sequence counter so the
bus can be restarted (e.g. after a gym environment ``reset()``).  Sinks
are *not* re-registered; call ``start()`` again on the same bus instance.
"""

from __future__ import annotations

import logging
from typing import Any

from .event_sinks import EventSink, InMemorySink

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level no-op publishers (zero body — fastest possible callable)
# ---------------------------------------------------------------------------


def _noop_publish_event(
    agent_id: int,
    agent_type: str,
    sim_time_ns: int,
    event_type: str,
    payload: Any,
) -> None:
    """No-op publish_event stub when no sink accepts events."""


def _noop_publish_metric(
    agent_id: int,
    agent_type: str,
    sim_time_ns: int,
    key: str,
    value: float,
) -> None:
    """No-op publish_metric stub when no sink accepts metrics."""


def _noop_publish_book_snapshot(
    symbol: str,
    sim_time_ns: int,
    bids: tuple,
    asks: tuple,
    depth: int,
) -> None:
    """No-op publish_book_snapshot stub when no sink accepts book snapshots."""


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Single-threaded event bus: append-during-handler, drain-after-handler.

    Typical kernel-side usage::

        bus = EventBus()
        bus.register(InMemorySink())
        bus.register(BZ2PickleSink(log_writer, agents))
        bus.register(MetricsObserverSink(observers))

        bus.start(meta={"sim_id": run_id})

        # Inside the kernel runner loop:
        agent.wakeup(current_time)
        bus.drain()

        bus.shutdown(meta={"sim_id": run_id})

    Agent-side usage::

        self.kernel.event_bus.publish_event(
            self.id, self.type, current_time, "ORDER_SUBMITTED", payload
        )
    """

    def __init__(self) -> None:
        self._sinks: list[EventSink] = []
        self._started: bool = False

        # Ring buffers — cleared after every drain()
        self._event_buf: list[tuple] = []
        self._metric_buf: list[tuple] = []
        self._book_snapshot_buf: list[tuple] = []

        # Pre-start queue: events published before bus.start() (e.g. during
        # Agent.__init__).  Drained at the beginning of start().
        self._pre_start_event_buf: list[tuple] = []
        self._pre_start_metric_buf: list[tuple] = []

        # Monotonic sequence counter (per bus, across all kinds)
        self._seq: int = 0

        # Failure tracking
        self._failed_sinks: set[int] = set()  # indices into self._sinks
        self._sink_failures: list[tuple[int, BaseException]] = []

        # Active-dispatch lists (filtered at start() time)
        self._event_sinks: list[tuple[int, EventSink]] = []
        self._metric_sinks: list[tuple[int, EventSink]] = []
        self._book_sinks: list[tuple[int, EventSink]] = []

        # Publish callables — rebound at start() time if no sinks accept a kind.
        # Before start() they go to the pre-start buffers via dedicated stubs.
        self.publish_event = self._pre_start_publish_event
        self.publish_metric = self._pre_start_publish_metric
        self.publish_book_snapshot = _noop_publish_book_snapshot

    # ---- Registration --------------------------------------------------------

    def register(self, sink: EventSink) -> None:
        """Register a sink.

        Must be called before :meth:`start`.  Raises :class:`RuntimeError`
        if called after the bus has been started.

        Arguments:
            sink: Any object satisfying the :class:`~abides_core.event_sinks.EventSink`
                Protocol.
        """
        if self._started:
            raise RuntimeError(
                "Cannot register sinks after EventBus.start() has been called."
            )
        self._sinks.append(sink)

    # ---- Pre-start stubs (before start() is called) --------------------------

    def _pre_start_publish_event(
        self,
        agent_id: int,
        agent_type: str,
        sim_time_ns: int,
        event_type: str,
        payload: Any,
    ) -> None:
        """Buffer events published before start() (e.g. from Agent.__init__)."""
        self._seq += 1
        self._pre_start_event_buf.append(
            (agent_id, agent_type, sim_time_ns, event_type, payload, self._seq)
        )

    def _pre_start_publish_metric(
        self,
        agent_id: int,
        agent_type: str,
        sim_time_ns: int,
        key: str,
        value: float,
    ) -> None:
        """Buffer metrics published before start()."""
        self._seq += 1
        self._pre_start_metric_buf.append(
            (agent_id, agent_type, sim_time_ns, key, value, self._seq)
        )

    # ---- Lifecycle -----------------------------------------------------------

    def start(self, meta: dict | None = None) -> None:
        """Start the bus: build active-dispatch lists and notify sinks.

        May be called multiple times (e.g. gym reset).  Each call clears
        ring buffers and failed-sink state.  Sinks are **not** re-registered.
        Pre-start queued events are drained to all sinks immediately after
        ``on_simulation_start``.

        Arguments:
            meta: Arbitrary metadata forwarded to ``sink.on_simulation_start``.
                  The kernel passes ``{"sim_id": run_id}`` here.
        """
        if meta is None:
            meta = {}

        # Reset runtime state (supports gym reuse)
        self._started = True
        self._failed_sinks = set()
        self._sink_failures = []
        self._event_buf = []
        self._metric_buf = []
        self._book_snapshot_buf = []

        # Build filtered dispatch lists: (index, sink) pairs
        self._event_sinks = [
            (i, s) for i, s in enumerate(self._sinks) if s.accept_events
        ]
        self._metric_sinks = [
            (i, s) for i, s in enumerate(self._sinks) if s.accept_metrics
        ]
        self._book_sinks = [
            (i, s) for i, s in enumerate(self._sinks) if s.accept_book_snapshots
        ]

        # Notify all sinks
        for i, sink in enumerate(self._sinks):
            self._call_sink(i, sink.on_simulation_start, meta)

        # Rebind publish_* based on which kinds have active sinks
        if self._event_sinks:
            self.publish_event = self._real_publish_event
        else:
            self.publish_event = _noop_publish_event

        if self._metric_sinks:
            self.publish_metric = self._real_publish_metric
        else:
            self.publish_metric = _noop_publish_metric

        if self._book_sinks:
            self.publish_book_snapshot = self._real_publish_book_snapshot
        else:
            self.publish_book_snapshot = _noop_publish_book_snapshot

        # Drain pre-start queues into the ring buffers and dispatch immediately
        if self._pre_start_event_buf or self._pre_start_metric_buf:
            self._event_buf.extend(self._pre_start_event_buf)
            self._metric_buf.extend(self._pre_start_metric_buf)
            self._pre_start_event_buf.clear()
            self._pre_start_metric_buf.clear()
            self._drain_buffers()

    # ---- Real publishers (bound at start() when sinks are active) ------------

    def _real_publish_event(
        self,
        agent_id: int,
        agent_type: str,
        sim_time_ns: int,
        event_type: str,
        payload: Any,
    ) -> None:
        self._seq += 1
        self._event_buf.append(
            (agent_id, agent_type, sim_time_ns, event_type, payload, self._seq)
        )

    def _real_publish_metric(
        self,
        agent_id: int,
        agent_type: str,
        sim_time_ns: int,
        key: str,
        value: float,
    ) -> None:
        self._seq += 1
        self._metric_buf.append(
            (agent_id, agent_type, sim_time_ns, key, value, self._seq)
        )

    def _real_publish_book_snapshot(
        self,
        symbol: str,
        sim_time_ns: int,
        bids: tuple,
        asks: tuple,
        depth: int,
    ) -> None:
        self._seq += 1
        self._book_snapshot_buf.append(
            (symbol, sim_time_ns, bids, asks, depth, self._seq)
        )

    # ---- Drain / flush / shutdown --------------------------------------------

    def drain(self) -> None:
        """Dispatch buffered tuples to active sinks and clear the buffers.

        Called by the kernel after every handler invocation (wakeup or
        receive_message).  Returns immediately if all buffers are empty
        (common case — the Python ``if not list`` check is a single
        reference comparison).
        """
        if not self._event_buf and not self._metric_buf and not self._book_snapshot_buf:
            return
        self._drain_buffers()

    def _drain_buffers(self) -> None:
        """Inner drain — called when at least one buffer is non-empty."""
        # Events
        if self._event_buf:
            buf = self._event_buf
            self._event_buf = []
            for i, sink in self._event_sinks:
                if i in self._failed_sinks:
                    continue
                for t in buf:
                    self._call_sink(i, sink.on_event, t)

        # Metrics
        if self._metric_buf:
            buf = self._metric_buf
            self._metric_buf = []
            for i, sink in self._metric_sinks:
                if i in self._failed_sinks:
                    continue
                for t in buf:
                    self._call_sink(i, sink.on_metric, t)

        # Book snapshots
        if self._book_snapshot_buf:
            buf = self._book_snapshot_buf
            self._book_snapshot_buf = []
            for i, sink in self._book_sinks:
                if i in self._failed_sinks:
                    continue
                for t in buf:
                    self._call_sink(i, sink.on_book_snapshot, t)

    def flush(self) -> None:
        """Request all non-failed sinks to flush to their backing store.

        Performs a :meth:`drain` first to ensure all buffered tuples are
        delivered before the flush request.  Callers can invoke this
        mid-simulation to force a checkpoint.
        """
        self.drain()
        for i, sink in enumerate(self._sinks):
            if i in self._failed_sinks:
                continue
            self._call_sink(i, sink.flush)

    def shutdown(self, meta: dict | None = None) -> None:
        """Drain remaining events and finalise all sinks.

        Called once by the kernel from ``terminate()``.  After this call
        the bus is in a terminal state; ``start()`` must be called again
        before publishing is possible.

        Arguments:
            meta: Same metadata dict as :meth:`start`.
        """
        if meta is None:
            meta = {}

        # Final drain
        self.drain()

        # Notify sinks of simulation end
        for i, sink in enumerate(self._sinks):
            if i in self._failed_sinks:
                continue
            self._call_sink(i, sink.on_simulation_end, meta)

        self._started = False

        # Rebind to pre-start stubs so the bus can be restarted (gym)
        self.publish_event = self._pre_start_publish_event
        self.publish_metric = self._pre_start_publish_metric
        self.publish_book_snapshot = _noop_publish_book_snapshot

        # Surface accumulated failures
        if self._sink_failures:
            msgs = [
                f"  sink[{idx}] ({type(self._sinks[idx]).__name__}): {exc}"
                for idx, exc in self._sink_failures
            ]
            raise RuntimeError(
                "EventBus: one or more sinks raised exceptions during the simulation:\n"
                + "\n".join(msgs)
            )

    # ---- Internal helpers ----------------------------------------------------

    def _call_sink(self, sink_index: int, method, *args: Any) -> None:
        """Call ``method(*args)`` and catch any exception.

        On the first exception from a sink the sink is added to
        ``_failed_sinks`` (so subsequent calls are skipped) and the
        exception is appended to ``_sink_failures`` for surface at
        ``shutdown()``.
        """
        try:
            method(*args)
        except Exception as exc:
            if sink_index not in self._failed_sinks:
                self._failed_sinks.add(sink_index)
                self._sink_failures.append((sink_index, exc))
                sink_type = type(self._sinks[sink_index]).__name__
                logger.error(
                    "EventBus: sink[%d] (%s) raised an exception and has been "
                    "removed from the active dispatch set: %s",
                    sink_index,
                    sink_type,
                    exc,
                    exc_info=True,
                )

    # ---- Convenience accessors -----------------------------------------------

    @property
    def in_memory_sink(self) -> InMemorySink | None:
        """Return the first registered :class:`~abides_core.event_sinks.InMemorySink`, or ``None``.

        Used by the ``Agent.log`` deprecation shim.
        """
        for sink in self._sinks:
            if isinstance(sink, InMemorySink):
                return sink
        return None

    @property
    def started(self) -> bool:
        """``True`` while the bus is between :meth:`start` and :meth:`shutdown`."""
        return self._started

    @property
    def failed_sink_count(self) -> int:
        """Number of sinks that failed during the current (or last) run."""
        return len(self._failed_sinks)

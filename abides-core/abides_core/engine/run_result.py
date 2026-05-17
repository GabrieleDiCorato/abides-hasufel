"""Lean, timing-only result returned from :meth:`Kernel.terminate`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from .. import NanosecondTime


@dataclass(frozen=True, slots=True)
class SinkFailure:
    """One sink-failure record surfaced from :class:`~abides_core.event_bus.EventBus`.

    Attributes:
      sink_index: Position of the sink in the bus's registered list.
      sink_type:  ``type(sink).__name__`` for human-readable diagnostics.
      exception_repr: ``repr(exc)`` of the first exception raised by the sink.
        Only the first failure per sink is recorded; subsequent batches
        for the same sink are silently skipped.
    """

    sink_index: int
    sink_type: str
    exception_repr: str


@dataclass(frozen=True, slots=True)
class KernelRunResult:
    """Scheduling facts for one completed kernel run.

    Attributes:
      elapsed: Wall-clock time spent inside :meth:`Kernel.runner`.
      slowest_agent_finish_time: The maximum
        ``Kernel._agent_current_times`` value at termination — the
        latest "agent-busy-until" timestamp observed.
      messages_processed: Total number of messages dequeued during
        the run.
      sink_failures: Tuple of :class:`SinkFailure` records for every
        event-bus sink that raised an exception during the run.  Empty
        on a clean run.  Lets programmatic callers detect partial
        telemetry loss (e.g. ``BZ2PickleSink`` hitting a full disk)
        without parsing logs.
    """

    elapsed: timedelta
    slowest_agent_finish_time: NanosecondTime
    messages_processed: int
    sink_failures: tuple[SinkFailure, ...] = field(default_factory=tuple)

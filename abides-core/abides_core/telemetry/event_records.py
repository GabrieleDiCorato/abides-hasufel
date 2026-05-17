"""Typed views of wire-tuple records produced by the event bus.

The event bus delivers events as plain Python tuples for zero-overhead
hot-path dispatch.  This module provides:

- **Wire-field-order constants** (``WIRE_FIELDS_*``) — the positional
  layout for each tuple kind.  These are **public semver-stable API**
  (§3.10 of the refactor plan): the positional layout may only change
  with a major version bump.
- **Typed record views** (``EventRecord``, ``MetricRecord``,
  ``BookSnapshotRecord``) — lightweight dataclasses that unpack a
  wire tuple by position.  Sinks that prefer named-attribute access
  use ``SomeRecord.from_tuple(t)``; performance-sensitive sinks read
  positional indices directly.

Wire-tuple shapes (bus injects ``seq``):

.. code-block:: text

    event tuple:        (agent_id, agent_type, sim_time_ns, event_type, payload, seq)
    metric tuple:       (agent_id, agent_type, sim_time_ns, key, value, seq)
    book_snapshot tuple:(symbol, sim_time_ns, bids, asks, depth, seq)

All tuples are **read-only**.  Sinks that need a private copy must
allocate one themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import NanosecondTime

# ---------------------------------------------------------------------------
# Public wire-field-order constants (semver-stable per §3.10)
# ---------------------------------------------------------------------------

WIRE_FIELDS_EVENT: tuple[str, ...] = (
    "agent_id",
    "agent_type",
    "sim_time_ns",
    "event_type",
    "payload",
    "seq",
)
"""Positional field names for event wire tuples.

Index:
    0 → agent_id     (int)
    1 → agent_type   (str)
    2 → sim_time_ns  (int, NanosecondTime)
    3 → event_type   (str)
    4 → payload      (Any — scalar, dict, list, or tuple depending on schema)
    5 → seq          (int, monotonic per bus)
"""

WIRE_FIELDS_METRIC: tuple[str, ...] = (
    "agent_id",
    "agent_type",
    "sim_time_ns",
    "key",
    "value",
    "seq",
)
"""Positional field names for metric wire tuples.

Index:
    0 → agent_id    (int)
    1 → agent_type  (str)
    2 → sim_time_ns (int, NanosecondTime)
    3 → key         (str — metric name)
    4 → value       (float)
    5 → seq         (int, monotonic per bus)
"""

WIRE_FIELDS_BOOK_SNAPSHOT: tuple[str, ...] = (
    "symbol",
    "sim_time_ns",
    "bids",
    "asks",
    "depth",
    "seq",
)
"""Positional field names for book-snapshot wire tuples.

Index:
    0 → symbol       (str)
    1 → sim_time_ns  (int, NanosecondTime)
    2 → bids         (tuple of (price_cents: int, qty: int) tuples)
    3 → asks         (tuple of (price_cents: int, qty: int) tuples)
    4 → depth        (int — number of price levels requested)
    5 → seq          (int, monotonic per bus)

``bids`` and ``asks`` are immutable tuples materialized eagerly at
publish time.  They carry **no back-references** to the live order book.
"""

# ---------------------------------------------------------------------------
# Typed record views
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EventRecord:
    """Typed view of an event wire tuple.

    Use :meth:`from_tuple` to construct from a raw bus tuple.  Prefer
    positional index access when performance matters.
    """

    agent_id: int
    agent_type: str
    sim_time_ns: NanosecondTime
    event_type: str
    payload: Any
    seq: int

    @classmethod
    def from_tuple(cls, t: tuple) -> EventRecord:
        """Unpack an event wire tuple into a typed record.

        Arguments:
            t: A 6-element tuple in ``WIRE_FIELDS_EVENT`` order.
        """
        return cls(
            agent_id=t[0],
            agent_type=t[1],
            sim_time_ns=t[2],
            event_type=t[3],
            payload=t[4],
            seq=t[5],
        )


@dataclass(slots=True)
class MetricRecord:
    """Typed view of a metric wire tuple.

    Use :meth:`from_tuple` to construct from a raw bus tuple.
    """

    agent_id: int
    agent_type: str
    sim_time_ns: NanosecondTime
    key: str
    value: float
    seq: int

    @classmethod
    def from_tuple(cls, t: tuple) -> MetricRecord:
        """Unpack a metric wire tuple into a typed record.

        Arguments:
            t: A 6-element tuple in ``WIRE_FIELDS_METRIC`` order.
        """
        return cls(
            agent_id=t[0],
            agent_type=t[1],
            sim_time_ns=t[2],
            key=t[3],
            value=t[4],
            seq=t[5],
        )


@dataclass(slots=True)
class BookSnapshotRecord:
    """Typed view of a book-snapshot wire tuple.

    Use :meth:`from_tuple` to construct from a raw bus tuple.
    """

    symbol: str
    sim_time_ns: NanosecondTime
    bids: tuple
    asks: tuple
    depth: int
    seq: int

    @classmethod
    def from_tuple(cls, t: tuple) -> BookSnapshotRecord:
        """Unpack a book-snapshot wire tuple into a typed record.

        Arguments:
            t: A 6-element tuple in ``WIRE_FIELDS_BOOK_SNAPSHOT`` order.
        """
        return cls(
            symbol=t[0],
            sim_time_ns=t[1],
            bids=t[2],
            asks=t[3],
            depth=t[4],
            seq=t[5],
        )

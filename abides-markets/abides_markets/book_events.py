"""Typed NamedTuple payloads for OrderBook events published on the EventBus.

Each event corresponds to one entry in the legacy ``OrderBook.history``
list.  The ``symbol`` is the first field so the per-symbol
:class:`~abides_core.event_sinks.OrderBookHistoryMemorySink` can
demultiplex events from a multi-symbol exchange (the wire tuple only
carries ``agent_id``, which is shared across symbols owned by the same
exchange).

The legacy ``time`` and ``type`` fields are dropped from the payload —
``time`` is carried by ``sim_time_ns`` in the wire tuple, and ``type``
is carried by ``event_type``.  The deprecated
``OrderBook.history`` cached property re-materializes the legacy dict
shape (including ``time`` and ``type``, with ``symbol`` stripped) for
backward compatibility with downstream consumers.
"""

from __future__ import annotations

from typing import NamedTuple


class LimitPayload(NamedTuple):
    symbol: str
    order_id: int
    agent_id: int
    side: str
    quantity: int
    price: int


class ExecPayload(NamedTuple):
    symbol: str
    order_id: int
    agent_id: int
    oppos_order_id: int
    oppos_agent_id: int
    side: str  # POV of the passive order being executed
    quantity: int
    price: int


class CancelPayload(NamedTuple):
    symbol: str
    order_id: int
    tag: str | None
    metadata: dict | None  # only set when tag == "auctionFill"


class CancelPartialPayload(NamedTuple):
    symbol: str
    order_id: int
    quantity: int
    tag: str | None
    metadata: dict | None


class ModifyPayload(NamedTuple):
    symbol: str
    order_id: int
    new_side: str
    new_quantity: int


class ReplacePayload(NamedTuple):
    symbol: str
    old_order_id: int
    new_order_id: int
    quantity: int
    price: int


BOOK_EVENT_PAYLOAD_CLASSES: dict[str, type[NamedTuple]] = {
    "LIMIT": LimitPayload,
    "EXEC": ExecPayload,
    "CANCEL": CancelPayload,
    "CANCEL_PARTIAL": CancelPartialPayload,
    "MODIFY": ModifyPayload,
    "REPLACE": ReplacePayload,
}
"""Event-type string -> payload NamedTuple class.

Exposed for sinks / consumers that need to validate or reconstruct
payloads.  The keys exactly match the ``event_type`` string passed to
``EventBus.publish_event``.
"""


__all__ = [
    "BOOK_EVENT_PAYLOAD_CLASSES",
    "CancelPartialPayload",
    "CancelPayload",
    "ExecPayload",
    "LimitPayload",
    "ModifyPayload",
    "ReplacePayload",
]

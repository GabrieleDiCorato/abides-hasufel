"""Payload schema registry for the ABIDES event bus.

Every ``event_type`` string produced by a shipped agent or order book has
a corresponding :class:`PayloadSchema` entry in :data:`EVENT_TYPE_SCHEMA`.

Schemas are **module-level singletons** — one instance per logical
payload shape, shared across many ``event_type`` strings.  No allocation
happens at publish time.

Public contract (semver-stable, see §3.10 of the refactor plan):
    - :class:`PayloadSchema` — the schema descriptor.
    - :data:`EVENT_TYPE_SCHEMA` — the single source of truth mapping
      ``event_type → schema``.
    - Named schema instances: ``ORDER_EVENT``, ``HOLDINGS``, ``CASH``,
      ``DEPTH``, ``QUOTE``, ``AGENT_TYPE_SCHEMA``, ``EMPTY``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PayloadSchema:
    """Descriptor for the payload carried by a named ``event_type``.

    Attributes:
        name:    Human-readable schema name (e.g. ``"ORDER_EVENT"``).
        version: Bumped on any field rename, addition, removal, or
                 reorder.  Consumers that persist payloads (e.g.
                 Parquet files) should record this and reject mismatches.
        fields:  Ordered tuple of field names for arity ≥ 2 payloads.
                 Empty for scalar and empty-payload schemas.

    Payload arity rules (match the §3.9 "scalar/tuple/empty" contract):
        - ``len(fields) == 0`` → payload is the shared ``()`` singleton
          (empty event, e.g. ``MKT_CLOSED``).
        - ``len(fields) == 1`` → payload is the bare scalar value named
          by ``fields[0]`` (e.g. ``STARTING_CASH`` carries an ``int``).
        - ``len(fields) >= 2`` → payload is a tuple whose positional
          slots correspond to ``fields``.

    This is not enforced at publish time in production builds.  Enable
    ``ABIDES_BUS_VALIDATE=1`` in the environment for debug-mode checks.
    """

    name: str
    version: int
    fields: tuple[str, ...]


# ---------------------------------------------------------------------------
# Shared schema instances (one per logical payload shape).
# ---------------------------------------------------------------------------

ORDER_EVENT = PayloadSchema(
    name="ORDER_EVENT",
    version=1,
    fields=(
        "order_id",
        "order_kind",
        "symbol",
        "side",
        "quantity",
        "limit_price",
        "stop_price",
        "time_in_force",
        "is_hidden",
        "is_price_to_comply",
        "tag",
    ),
)
"""Shared schema for all order lifecycle events.

In Phase 2 the payload is still the ``dict`` returned by
``Order.to_dict()``.  Migration to a typed tuple (dropping the dict)
is deferred to Phase 2a when ``Order.to_payload_tuple()`` lands.

Consumers must access the ``dict`` payload directly until Phase 2a;
the ``fields`` tuple documents the *intended* final positional layout.
"""

HOLDINGS = PayloadSchema(
    name="HOLDINGS",
    version=1,
    fields=("holdings_dict",),
)
"""Schema for ``HOLDINGS_UPDATED``.

In Phase 2 the payload is still the full ``dict[str, int]`` holdings
snapshot (deep-copied by the producer).  Migration to a per-fill delta
tuple ``(symbol, delta_qty, qty_after, cash_after_cents)`` is deferred
to Phase 2a.
"""

CASH = PayloadSchema(
    name="CASH",
    version=1,
    fields=("cents",),
)
"""Scalar cash / mark-to-market amount in integer cents."""

DEPTH = PayloadSchema(
    name="DEPTH",
    version=1,
    fields=("levels",),
)
"""Order-book depth snapshot.

Payload is a ``list`` of ``(price_cents, qty)`` tuples.
"""

QUOTE = PayloadSchema(
    name="QUOTE",
    version=1,
    fields=("symbol", "price_cents", "qty"),
)
"""Best-bid / best-ask / last-trade quote.

In Phase 2 the payload for ``BEST_BID`` / ``BEST_ASK`` is the formatted
string emitted by ``OrderBook`` (e.g. ``"ABM,10000,100"``), and for
``LAST_TRADE`` it is a similarly formatted string.  Migration to a
structured tuple is deferred to Phase 2a.
"""

AGENT_TYPE_SCHEMA = PayloadSchema(
    name="AGENT_TYPE",
    version=1,
    fields=("name",),
)
"""Single-string schema for the ``AGENT_TYPE`` lifecycle event."""

EMPTY = PayloadSchema(
    name="EMPTY",
    version=1,
    fields=(),
)
"""Empty payload — the event is a marker with no data.

Producers should pass ``()`` (the shared empty tuple singleton).
"""

SUMMARY = PayloadSchema(
    name="SUMMARY",
    version=1,
    fields=("text",),
)
"""Formatted-string summary (e.g. ``FINAL_HOLDINGS``, ``MARK_TO_MARKET``).

These events currently carry human-readable formatted strings.  They are
retained as-is for Phase 2 and tagged for structured-payload migration in
Phase 2a.
"""

VALUATION = PayloadSchema(
    name="VALUATION",
    version=1,
    fields=("value",),
)
"""Scalar valuation (surplus fraction or cents).

``FINAL_VALUATION`` uses this schema.  The payload type is
``int`` (cents) for one call site and ``float`` (surplus fraction) for
others — see the ``[REVIEW]`` note in ``event-vocabulary.md``.
"""

EXECUTION_SUMMARY = PayloadSchema(
    name="EXECUTION_SUMMARY",
    version=1,
    fields=(
        "executed_quantity",
        "target_quantity",
        "remaining_quantity",
        "execution_rate",
    ),
)
"""End-of-run execution summary for execution agents."""

SLICE_DECISION = PayloadSchema(
    name="SLICE_DECISION",
    version=1,
    fields=("time", "order_size", "remaining_quantity", "direction"),
)
"""Per-slice decision record for execution agents."""

POV_SUMMARY = PayloadSchema(
    name="POV_SUMMARY",
    version=1,
    fields=("effective_pov", "total_market_volume"),
)
"""End-of-run POV participation summary."""

AMM_FLATTEN = PayloadSchema(
    name="AMM_FLATTEN",
    version=1,
    fields=("symbol", "position_closed"),
)
"""Position-flatten event for the adaptive market maker."""

CIRCUIT_BREAKER = PayloadSchema(
    name="CIRCUIT_BREAKER",
    version=1,
    fields=("reason", "value"),
)
"""Circuit-breaker trip record.

The ``reason`` field is ``"max_drawdown"`` or ``"max_order_rate"``;
``value`` is the triggering loss or order count.
"""

GENERIC = PayloadSchema(
    name="GENERIC",
    version=1,
    fields=("payload",),
)
"""Catch-all schema for event types not yet in the registry.

The ``InMemorySink`` routes unknown event types here and emits a
one-time warning.  No event type produced by shipped agents should
ever land here — it signals a missing registry entry.
"""


# ---------------------------------------------------------------------------
# The single source of truth: event_type → schema.
# ---------------------------------------------------------------------------

EVENT_TYPE_SCHEMA: dict[str, PayloadSchema] = {
    # --- Order lifecycle (TradingAgent) ---
    "ORDER_SUBMITTED": ORDER_EVENT,
    "ORDER_ACCEPTED": ORDER_EVENT,
    "ORDER_EXECUTED": ORDER_EVENT,
    "ORDER_CANCELLED": ORDER_EVENT,
    "PARTIAL_CANCELLED": ORDER_EVENT,
    "ORDER_MODIFIED": ORDER_EVENT,
    "ORDER_REPLACED": ORDER_EVENT,
    "CANCEL_SUBMITTED": ORDER_EVENT,
    "CANCEL_PARTIAL_ORDER": ORDER_EVENT,
    "MODIFY_ORDER": ORDER_EVENT,
    "REPLACE_ORDER": ORDER_EVENT,
    # --- Stop orders ---
    "STOP_ORDER_SUBMITTED": ORDER_EVENT,
    "STOP_ORDER_ACCEPTED": SUMMARY,  # exchange emits str(order) — see [REVIEW]
    "STOP_TRIGGERED": ORDER_EVENT,
    # --- Holdings / cash (TradingAgent) ---
    "STARTING_CASH": CASH,
    "FINAL_CASH_POSITION": CASH,
    "ENDING_CASH": CASH,
    "HOLDINGS_UPDATED": HOLDINGS,
    "FINAL_HOLDINGS": SUMMARY,  # formatted string — see [REVIEW]
    "MARK_TO_MARKET": SUMMARY,  # formatted string — see [REVIEW]
    "MARKED_TO_MARKET": CASH,
    # --- Market data echo (TradingAgent) ---
    "BID_DEPTH": DEPTH,
    "ASK_DEPTH": DEPTH,
    "IMBALANCE": DEPTH,
    # --- Order book (ExchangeAgent via OrderBook) ---
    "BEST_BID": QUOTE,  # Phase 2: payload is still the formatted string
    "BEST_ASK": QUOTE,  # Phase 2: payload is still the formatted string
    "LAST_TRADE": QUOTE,  # Phase 2: payload is still the formatted string
    # --- Lifecycle ---
    "AGENT_TYPE": AGENT_TYPE_SCHEMA,
    "MKT_CLOSED": EMPTY,
    # --- Valuation ---
    "FINAL_VALUATION": VALUATION,
    # --- Execution agents ---
    "EXECUTION_SUMMARY": EXECUTION_SUMMARY,
    "SLICE_DECISION": SLICE_DECISION,
    "POV_SUMMARY": POV_SUMMARY,
    # --- Market makers ---
    "AMM_FLATTEN": AMM_FLATTEN,
    # --- Risk ---
    "CIRCUIT_BREAKER_TRIPPED": CIRCUIT_BREAKER,
}
"""Complete map from ``event_type`` string to :class:`PayloadSchema`.

Dynamic-name events (``ExchangeAgent`` message-type events, order-book
``<tag>_POST_ONLY`` events) are not enumerable statically — they do not
appear here.  The ``InMemorySink`` falls back to :data:`GENERIC` for
any event type absent from this dict.
"""

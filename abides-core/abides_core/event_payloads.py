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

The payload is the positional tuple returned by
:meth:`abides_markets.orders.Order.to_payload_tuple`. ``side`` and
``time_in_force`` are :class:`~enum.IntEnum` members; consumer-side
string formatting (when needed) calls ``.legacy_str()``.
"""

HOLDINGS = PayloadSchema(
    name="HOLDINGS",
    version=1,
    fields=("holdings_dict",),
)
"""Schema for ``HOLDINGS_UPDATED``.

Payload is the full ``dict[str, int]`` holdings snapshot.
Migration to a per-fill delta tuple
``(symbol, delta_qty, qty_after, cash_after_cents)`` is deferred to
Phase 2c so that snapshot-diff consumers have a release window and a
``reconstruct_holdings()`` helper to migrate against.
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

Payload is the positional tuple ``(symbol, price_cents, qty)`` where
``price_cents`` and ``qty`` may be ``None`` when the relevant book side
is empty (e.g. ``BEST_BID`` with no resting bids).
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

Producers should pass :data:`EMPTY_PAYLOAD` (the shared empty tuple
singleton) rather than allocating a new tuple per publish.
"""

EMPTY_PAYLOAD: tuple[()] = ()
"""Shared empty-tuple singleton for arity-0 events.

Use this constant rather than ``()`` literal so the intent of an
``EMPTY``-schema event is clear at the call site.
"""

SUMMARY = PayloadSchema(
    name="SUMMARY",
    version=1,
    fields=("text",),
)
"""Free-form human-readable summary string.

Reserved for events whose only purpose is operator-visible logging.
After Phase 2b only ``FINAL_HOLDINGS`` uses this shape; structured
counterparts (``MARKED_TO_MARKET``) carry numeric scalars under
:data:`CASH`.
"""

IMBALANCE_PAYLOAD = PayloadSchema(
    name="IMBALANCE",
    version=1,
    fields=("bid_total_qty", "ask_total_qty"),
)
"""Two-element depth imbalance summary.

Payload is ``(sum_of_bid_quantities, sum_of_ask_quantities)``. Distinct
from :data:`DEPTH`, which carries the full per-level array.
"""

FILL_PNL = PayloadSchema(
    name="FILL_PNL",
    version=1,
    fields=("nav", "peak_nav", "symbol"),
)
"""Per-fill mark-to-market snapshot used by the circuit-breaker.

Payload is ``(nav_cents, peak_nav_cents, symbol)``.
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
    "STOP_ORDER_ACCEPTED": ORDER_EVENT,
    "STOP_TRIGGERED": ORDER_EVENT,
    # --- ExchangeAgent message-type echoes (Message.type() names) ---
    # ExchangeAgent.receive_message and ExchangeAgent.send_message echo
    # OrderMsg / OrderBookMsg subclasses under the message class name as
    # the event_type, with order.to_dict() (now to_payload_tuple()) as
    # the payload. The allowlist below mirrors the explicit dispatch in
    # ExchangeAgent.receive_message after Phase 2b Step 5.
    "LimitOrderMsg": ORDER_EVENT,
    "MarketOrderMsg": ORDER_EVENT,
    "CancelOrderMsg": ORDER_EVENT,
    "PartialCancelOrderMsg": ORDER_EVENT,
    "ModifyOrderMsg": ORDER_EVENT,
    "ReplaceOrderMsg": ORDER_EVENT,
    "OrderAcceptedMsg": ORDER_EVENT,
    "OrderExecutedMsg": ORDER_EVENT,
    "OrderCancelledMsg": ORDER_EVENT,
    "OrderPartialCancelledMsg": ORDER_EVENT,
    "OrderModifiedMsg": ORDER_EVENT,
    "OrderReplacedMsg": ORDER_EVENT,
    # --- ExchangeAgent non-order message echoes (Phase 2b Step 5) ---
    # These are query/subscription requests echoed by ExchangeAgent.
    # No structured payload is attached; the event is a bare receipt
    # under the EMPTY schema (sender_id is recorded by the sink).
    "QueryLastTradeMsg": EMPTY,
    "QuerySpreadMsg": EMPTY,
    "QueryOrderStreamMsg": EMPTY,
    "QueryTransactedVolMsg": EMPTY,
    "MarketHoursRequestMsg": EMPTY,
    "MarketClosePriceRequestMsg": EMPTY,
    "L1SubReqMsg": EMPTY,
    "L2SubReqMsg": EMPTY,
    "L3SubReqMsg": EMPTY,
    "TransactedVolSubReqMsg": EMPTY,
    "BookImbalanceSubReqMsg": EMPTY,
    # --- Holdings / cash (TradingAgent) ---
    "STARTING_CASH": CASH,
    "FINAL_CASH_POSITION": CASH,
    "ENDING_CASH": CASH,
    "HOLDINGS_UPDATED": HOLDINGS,
    "FINAL_HOLDINGS": SUMMARY,
    "MARK_TO_MARKET": CASH,  # per-symbol contribution in cents; structured counterpart is MARKED_TO_MARKET
    "MARKED_TO_MARKET": CASH,
    "FILL_PNL": FILL_PNL,
    # --- Market data echo (TradingAgent) ---
    "BID_DEPTH": DEPTH,
    "ASK_DEPTH": DEPTH,
    "IMBALANCE": IMBALANCE_PAYLOAD,
    # --- Order book (ExchangeAgent via OrderBook) ---
    "BEST_BID": QUOTE,
    "BEST_ASK": QUOTE,
    "LAST_TRADE": QUOTE,
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

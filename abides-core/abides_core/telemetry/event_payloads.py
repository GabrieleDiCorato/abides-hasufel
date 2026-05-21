"""Payload schema registry for the ABIDES event bus.

Every ``event_type`` string produced by a shipped agent or order book has
a corresponding :class:`PayloadSchema` entry in :data:`EVENT_TYPE_SCHEMA`.

Schemas are **module-level singletons** — one instance per logical
payload shape, shared across many ``event_type`` strings.  No allocation
happens at publish time.

Public contract (semver-stable):
    - :class:`EventType` — :class:`~enum.StrEnum` of all statically-known
      event type strings.  Use these constants at call sites instead of raw
      string literals so typos become import-time errors.
    - :class:`PayloadSchema` — the schema descriptor.
    - :data:`EVENT_TYPE_SCHEMA` — the single source of truth mapping
      ``EventType → schema``.
    - Named schema instances: ``ORDER_EVENT``, ``HOLDINGS_DELTA``,
      ``CASH``, ``DEPTH``, ``QUOTE``, ``AGENT_TYPE_SCHEMA``, ``EMPTY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EventType(StrEnum):
    """All statically-known event type strings produced by shipped agents.

    Because :class:`EventType` is a :class:`~enum.StrEnum`, every member
    *is* a ``str`` — existing comparisons such as
    ``logs_df[logs_df.EventType == "ORDER_SUBMITTED"]`` continue to work
    without change.

    Dynamic event types that cannot be enumerated statically (e.g.
    ``<order_tag>_POST_ONLY`` from the order book) are passed as plain
    ``str`` to :meth:`~abides_core.agent.Agent.logEvent` and are not
    listed here.
    """

    # --- Order lifecycle (TradingAgent) ---
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_EXECUTED = "ORDER_EXECUTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    PARTIAL_CANCELLED = "PARTIAL_CANCELLED"
    ORDER_MODIFIED = "ORDER_MODIFIED"
    ORDER_REPLACED = "ORDER_REPLACED"
    CANCEL_SUBMITTED = "CANCEL_SUBMITTED"
    CANCEL_PARTIAL_ORDER = "CANCEL_PARTIAL_ORDER"
    MODIFY_ORDER = "MODIFY_ORDER"
    REPLACE_ORDER = "REPLACE_ORDER"
    # --- Stop orders ---
    STOP_ORDER_SUBMITTED = "STOP_ORDER_SUBMITTED"
    STOP_ORDER_ACCEPTED = "STOP_ORDER_ACCEPTED"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    # --- ExchangeAgent message-type echoes ---
    LimitOrderMsg = "LimitOrderMsg"
    MarketOrderMsg = "MarketOrderMsg"
    CancelOrderMsg = "CancelOrderMsg"
    PartialCancelOrderMsg = "PartialCancelOrderMsg"
    ModifyOrderMsg = "ModifyOrderMsg"
    ReplaceOrderMsg = "ReplaceOrderMsg"
    OrderAcceptedMsg = "OrderAcceptedMsg"
    OrderExecutedMsg = "OrderExecutedMsg"
    OrderCancelledMsg = "OrderCancelledMsg"
    OrderPartialCancelledMsg = "OrderPartialCancelledMsg"
    OrderModifiedMsg = "OrderModifiedMsg"
    OrderReplacedMsg = "OrderReplacedMsg"
    # --- ExchangeAgent non-order message echoes ---
    QueryLastTradeMsg = "QueryLastTradeMsg"
    QuerySpreadMsg = "QuerySpreadMsg"
    QueryOrderStreamMsg = "QueryOrderStreamMsg"
    QueryTransactedVolMsg = "QueryTransactedVolMsg"
    MarketHoursRequestMsg = "MarketHoursRequestMsg"
    MarketClosePriceRequestMsg = "MarketClosePriceRequestMsg"
    L1SubReqMsg = "L1SubReqMsg"
    L2SubReqMsg = "L2SubReqMsg"
    L3SubReqMsg = "L3SubReqMsg"
    TransactedVolSubReqMsg = "TransactedVolSubReqMsg"
    BookImbalanceSubReqMsg = "BookImbalanceSubReqMsg"
    # --- Holdings / cash (TradingAgent) ---
    STARTING_CASH = "STARTING_CASH"
    FINAL_CASH_POSITION = "FINAL_CASH_POSITION"
    ENDING_CASH = "ENDING_CASH"
    HOLDINGS_UPDATED = "HOLDINGS_UPDATED"
    FINAL_HOLDINGS = "FINAL_HOLDINGS"
    MARK_TO_MARKET = "MARK_TO_MARKET"
    MARKED_TO_MARKET = "MARKED_TO_MARKET"
    FILL_PNL = "FILL_PNL"
    # --- Market data echo (TradingAgent) ---
    BID_DEPTH = "BID_DEPTH"
    ASK_DEPTH = "ASK_DEPTH"
    IMBALANCE = "IMBALANCE"
    # --- Order book (OrderBook via ExchangeAgent) ---
    BEST_BID = "BEST_BID"
    BEST_ASK = "BEST_ASK"
    LAST_TRADE = "LAST_TRADE"
    # --- Lifecycle ---
    AGENT_TYPE = "AGENT_TYPE"
    MKT_CLOSED = "MKT_CLOSED"
    # --- Valuation ---
    FINAL_VALUATION = "FINAL_VALUATION"
    # --- Execution agents ---
    EXECUTION_SUMMARY = "EXECUTION_SUMMARY"
    SLICE_DECISION = "SLICE_DECISION"
    POV_SUMMARY = "POV_SUMMARY"
    # --- Market makers ---
    AMM_FLATTEN = "AMM_FLATTEN"
    # --- Risk ---
    CIRCUIT_BREAKER_TRIPPED = "CIRCUIT_BREAKER_TRIPPED"


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

HOLDINGS_DELTA = PayloadSchema(
    name="HOLDINGS_DELTA",
    version=2,
    fields=("symbol", "delta_qty", "qty_after", "cash_after_cents"),
)
"""Schema for the ``HOLDINGS_UPDATED`` event.

Payload is the per-fill delta tuple
``(symbol, delta_qty, qty_after, cash_after_cents)``. ``delta_qty`` is
signed (positive for buy fills, negative for sell fills). At
``first_wake`` one row per held symbol is emitted with
``delta_qty == qty_after`` so the on-wire stream is self-contained.

Use :func:`abides_markets.utils.reconstruct_holdings` to fold the
per-fill deltas back into the legacy ``dict[str, int]`` snapshot.

Previously the payload was the full ``dict[str, int]`` holdings
snapshot under a schema named ``HOLDINGS``; that schema has been
removed.
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
Only ``FINAL_HOLDINGS`` currently uses this shape; structured
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

EVENT_TYPE_SCHEMA: dict[EventType, PayloadSchema] = {
    # --- Order lifecycle (TradingAgent) ---
    EventType.ORDER_SUBMITTED: ORDER_EVENT,
    EventType.ORDER_ACCEPTED: ORDER_EVENT,
    EventType.ORDER_EXECUTED: ORDER_EVENT,
    EventType.ORDER_CANCELLED: ORDER_EVENT,
    EventType.PARTIAL_CANCELLED: ORDER_EVENT,
    EventType.ORDER_MODIFIED: ORDER_EVENT,
    EventType.ORDER_REPLACED: ORDER_EVENT,
    EventType.CANCEL_SUBMITTED: ORDER_EVENT,
    EventType.CANCEL_PARTIAL_ORDER: ORDER_EVENT,
    EventType.MODIFY_ORDER: ORDER_EVENT,
    EventType.REPLACE_ORDER: ORDER_EVENT,
    # --- Stop orders ---
    EventType.STOP_ORDER_SUBMITTED: ORDER_EVENT,
    EventType.STOP_ORDER_ACCEPTED: ORDER_EVENT,
    EventType.STOP_TRIGGERED: ORDER_EVENT,
    # --- ExchangeAgent message-type echoes (Message.type() names) ---
    # ExchangeAgent.receive_message and ExchangeAgent.send_message echo
    # OrderMsg / OrderBookMsg subclasses under the message class name as
    # the event_type, with order.to_dict() (now to_payload_tuple()) as
    # the payload. The allowlist below mirrors the explicit dispatch in
    # ExchangeAgent.receive_message.
    EventType.LimitOrderMsg: ORDER_EVENT,
    EventType.MarketOrderMsg: ORDER_EVENT,
    EventType.CancelOrderMsg: ORDER_EVENT,
    EventType.PartialCancelOrderMsg: ORDER_EVENT,
    EventType.ModifyOrderMsg: ORDER_EVENT,
    EventType.ReplaceOrderMsg: ORDER_EVENT,
    EventType.OrderAcceptedMsg: ORDER_EVENT,
    EventType.OrderExecutedMsg: ORDER_EVENT,
    EventType.OrderCancelledMsg: ORDER_EVENT,
    EventType.OrderPartialCancelledMsg: ORDER_EVENT,
    EventType.OrderModifiedMsg: ORDER_EVENT,
    EventType.OrderReplacedMsg: ORDER_EVENT,
    # --- ExchangeAgent non-order message echoes ---
    # These are query/subscription requests echoed by ExchangeAgent.
    # No structured payload is attached; the event is a bare receipt
    # under the EMPTY schema (sender_id is recorded by the sink).
    EventType.QueryLastTradeMsg: EMPTY,
    EventType.QuerySpreadMsg: EMPTY,
    EventType.QueryOrderStreamMsg: EMPTY,
    EventType.QueryTransactedVolMsg: EMPTY,
    EventType.MarketHoursRequestMsg: EMPTY,
    EventType.MarketClosePriceRequestMsg: EMPTY,
    EventType.L1SubReqMsg: EMPTY,
    EventType.L2SubReqMsg: EMPTY,
    EventType.L3SubReqMsg: EMPTY,
    EventType.TransactedVolSubReqMsg: EMPTY,
    EventType.BookImbalanceSubReqMsg: EMPTY,
    # --- Holdings / cash (TradingAgent) ---
    EventType.STARTING_CASH: CASH,
    EventType.FINAL_CASH_POSITION: CASH,
    EventType.ENDING_CASH: CASH,
    EventType.HOLDINGS_UPDATED: HOLDINGS_DELTA,
    EventType.FINAL_HOLDINGS: SUMMARY,
    EventType.MARK_TO_MARKET: CASH,  # per-symbol contribution in cents; structured counterpart is MARKED_TO_MARKET
    EventType.MARKED_TO_MARKET: CASH,
    EventType.FILL_PNL: FILL_PNL,
    # --- Market data echo (TradingAgent) ---
    EventType.BID_DEPTH: DEPTH,
    EventType.ASK_DEPTH: DEPTH,
    EventType.IMBALANCE: IMBALANCE_PAYLOAD,
    # --- Order book (ExchangeAgent via OrderBook) ---
    EventType.BEST_BID: QUOTE,
    EventType.BEST_ASK: QUOTE,
    EventType.LAST_TRADE: QUOTE,
    # --- Lifecycle ---
    EventType.AGENT_TYPE: AGENT_TYPE_SCHEMA,
    EventType.MKT_CLOSED: EMPTY,
    # --- Valuation ---
    EventType.FINAL_VALUATION: VALUATION,
    # --- Execution agents ---
    EventType.EXECUTION_SUMMARY: EXECUTION_SUMMARY,
    EventType.SLICE_DECISION: SLICE_DECISION,
    EventType.POV_SUMMARY: POV_SUMMARY,
    # --- Market makers ---
    EventType.AMM_FLATTEN: AMM_FLATTEN,
    # --- Risk ---
    EventType.CIRCUIT_BREAKER_TRIPPED: CIRCUIT_BREAKER,
}
"""Complete map from ``event_type`` string to :class:`PayloadSchema`.

Dynamic-name events (``ExchangeAgent`` message-type events, order-book
``<tag>_POST_ONLY`` events) are not enumerable statically — they do not
appear here.  The ``InMemorySink`` falls back to :data:`GENERIC` for
any event type absent from this dict.
"""

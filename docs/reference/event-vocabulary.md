# Event Vocabulary

The canonical set of event type strings is defined in
`EventType` — a `StrEnum` in `abides_core.telemetry.event_payloads`.
Every member IS a `str`, so existing DataFrame comparisons such as
`logs_df[logs_df.EventType == "ORDER_SUBMITTED"]` remain valid.

Payload shapes for each member are governed by the co-located
`EVENT_TYPE_SCHEMA: dict[EventType, PayloadSchema]` registry, validated
at import time by `abides_core/tests/test_event_payload_schema.py`.

To find where an event is produced: `grep -rn EventType.<name>` across
the workspace. To understand its payload schema: look up the member in
`EVENT_TYPE_SCHEMA` and read the corresponding `PayloadSchema` singleton.

Dynamic event types not in `EventType` (those that cannot be enumerated
statically):
- `<order_tag>_POST_ONLY` — emitted by `OrderBook` on post-only rejections
  (`order_book.py`). The prefix is the order's user-defined tag.

## Known consumers

| Consumer | Location | What it reads |
|---|---|---|
| `parse_logs_df` | `abides-core/abides_core/utils.py` | `EventType` + `event_dict` keys projected to columns |
| `SimulationResult.order_logs` | `abides-markets/abides_markets/simulation/result.py` | Filters on `_ORDER_EVENT_TYPES` |
| `metrics._compute_per_agent_order_stats` | `abides-markets/abides_markets/simulation/metrics.py` | Branches on `ORDER_SUBMITTED`, `ORDER_EXECUTED`, `ORDER_CANCELLED` |
| `reconstruct_holdings` | `abides-markets/abides_markets/utils.py` | Folds `HOLDINGS_UPDATED` delta rows into a snapshot dict |
| Tests | `abides-core/tests/`, `abides-markets/tests/` | Assert specific event types; treat as binding contract |
| External readers | — | Any agent log is part of the public vocabulary; renames require a deprecation cycle |

## Notes on specific members

**`AGENT_TYPE`** — published via `publish_event` directly (not `logEvent`) in
`Agent.kernel_initializing()`. Always the first event row per agent.

**`FINAL_VALUATION`** — payload type is `int` (cents) at one call site in
`noise_agent.py` and `float` (surplus fraction) at others. The call
sites disagree on unit; tagged `[REVIEW]` for future cleanup.

**`MARK_TO_MARKET`** — per-symbol contribution in cents emitted once per symbol
at end-of-day; the display-string form is used at some sites. Tagged
`[REVIEW]` for future cleanup to a dict payload.

**`<message.type()>` echoes in `ExchangeAgent`** — the exchange logs inbound
order messages using `message.type()` as the `event_type`. These class
names (`LimitOrderMsg`, `MarketOrderMsg`, etc.) ARE `EventType` members
and appear in `EVENT_TYPE_SCHEMA`; they are passed as plain `str` at
runtime via `message.type()`.

**`report_metric`** — `Agent.report_metric()` writes to a separate metric
store (not the event bus). The only production caller in shipped agents
is `trading_agent.py` (`ending_value`, once per agent per sim).

## OrderBook events on the EventBus

These event types are published by `OrderBook` directly onto `EventBus`
(not via `Agent.logEvent`), with `agent_id=exchange.id` and
`agent_type="ExchangeAgent"` on the wire tuple. Payloads are typed
`NamedTuple` subclasses from `abides_markets.book_events`.

| event_type | payload `NamedTuple` | consumers |
|---|---|---|
| `LIMIT` | `LimitPayload(symbol, order_id, agent_id, side, quantity, price)` | `OrderBookHistoryMemorySink`; `ExchangeAgent._handle_query_order_stream` |
| `EXEC` | `ExecPayload(symbol, order_id, agent_id, oppos_order_id, oppos_agent_id, side, quantity, price)` | `OrderBookHistoryMemorySink`; `runner._extract_liquidity` / `_extract_trades` |
| `CANCEL` | `CancelPayload(symbol, order_id, tag, metadata)` | `OrderBookHistoryMemorySink`; `ExchangeAgent._handle_query_order_stream` |
| `CANCEL_PARTIAL` | `CancelPartialPayload(symbol, order_id, quantity, tag, metadata)` | same as `CANCEL` |
| `MODIFY` | `ModifyPayload(symbol, order_id, new_side, new_quantity)` | same as `CANCEL` |
| `REPLACE` | `ReplacePayload(symbol, old_order_id, new_order_id, quantity, price)` | same as `CANCEL` |

Snapshot publishes use `publish_book_snapshot` (not `publish_event`) and
carry no `event_type` string. See
[logging-architecture.md §6](logging-architecture.md#6-orderbook-capture-on-the-bus)
for the full producer/sink contract.

## Cross-references

- [logging-architecture.md](logging-architecture.md) — log writer / parser pipeline
- [data-extraction.md](data-extraction.md) — `parse_logs_df` and book history extraction

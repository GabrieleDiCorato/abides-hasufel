# Event vocabulary audit

> **Status:** Inventory only. Every entry is dispositioned `keep`.
> Suggested consolidations are marked `[REVIEW]` and require explicit
> sign-off before any rename, merge, or deletion lands.
>
> **Conservative-default rule.** ABIDES is consumed as a library by
> external researchers who parse pickled `agent.log` lists, raw
> `parse_logs_df` output, and the `EventType` strings emitted to
> `OrderLogsSchema`. Renaming or removing a public-facing event type is
> a breaking change. This audit therefore renames nothing on its own; it
> is a *map* of the current vocabulary, plus a list of candidates a
> future phase can take to a deprecation cycle.

> **Phase 2b update (schemas are now current, not proposed).** The
> per-event "proposed schema" column below is no longer aspirational
> for the entries that have been migrated. The canonical, build-time
> enforced contract now lives in
> [`abides_core.event_payloads.EVENT_TYPE_SCHEMA`][esrc] and is
> validated by [`test_event_payload_schema.py`][asrc].
> Key shape changes shipped in Phase 2b:
>
> * **`ORDER_EVENT` family** (`ORDER_SUBMITTED`, `ORDER_ACCEPTED`,
>   `ORDER_EXECUTED`, `ORDER_CANCELLED`, `PARTIAL_CANCELLED`,
>   `ORDER_MODIFIED`, `ORDER_REPLACED`, `CANCEL_SUBMITTED`,
>   `CANCEL_PARTIAL_ORDER`, `MODIFY_ORDER`, `REPLACE_ORDER`,
>   `STOP_ORDER_SUBMITTED`, `STOP_TRIGGERED`, `STOP_ORDER_ACCEPTED`,
>   plus the dynamic `<message.type()>` echoes from `ExchangeAgent`)
>   now ship the 11-field `ORDER_EVENT` positional tuple produced by
>   `order.to_payload_tuple()`. `Side` and `TimeInForce` are `IntEnum`
>   and cross the wire as integers (legacy text via
>   `Side.legacy_str()` / `TimeInForce.legacy_str()`).
> * **`BEST_BID` / `BEST_ASK`** now ship `(symbol, price, qty)`;
>   **`LAST_TRADE`** now ships `(symbol, avg_price_cents, qty)`. The
>   CSV-in-string forms are gone.
> * **`CIRCUIT_BREAKER_TRIPPED`** now ships `(reason, value)` — the
>   second slot is the previously asymmetric `loss` / `orders` integer,
>   harmonised under one field.
> * **`MARK_TO_MARKET`** now ships a bare integer (cents) under the
>   `CASH` schema; the per-symbol breakdown that used to share the name
>   is now a `logger.debug` line, not a logged event.
> * **`FILL_PNL`** ships `(nav, peak_nav, symbol)`.
> * **`MKT_CLOSED`**, **`AGENT_TYPE`** and all bare receipt echoes on
>   `ExchangeAgent` ship the `EMPTY_PAYLOAD` singleton (`()`), routed
>   through the `EMPTY` schema.
> * **`EXECUTION_SUMMARY`**, **`SLICE_DECISION`**, **`POV_SUMMARY`**
>   and **`AMM_FLATTEN`** now ship positional tuples matching their
>   registered schemas.
>
> Producers whose payload remains a `dict` (the dynamic
> `<tag>_POST_ONLY` rejection events) intentionally fall through to
> the `GENERIC` schema. `HOLDINGS_UPDATED` was reshaped to the
> `HOLDINGS_DELTA` schema in Phase 2c: the payload is now the
> positional tuple `(symbol, delta_qty, qty_after, cash_after_cents)`.
> Use `abides_markets.utils.reconstruct_holdings` to fold per-fill
> deltas back into a holdings snapshot.
>
> `parse_logs_df()` projects each schema back into per-field DataFrame
> columns so existing notebook code keeps working. See
> [`docs/reference/logging-architecture.md`](logging-architecture.md)
> §4.3.1 for the projection rules and the build-time AST audit.
>
> [esrc]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/event_payloads.py
> [asrc]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/tests/test_event_payload_schema.py

This document is a complete, source-anchored enumeration of every
`Agent.logEvent(...)` and `Agent.report_metric(...)` call site shipped
with ABIDES, grouped by producer category.

## How to read the tables

| Column | Meaning |
|---|---|
| `event_type` | Literal first arg to `logEvent()` (or metric name for `report_metric`). |
| `producer (file:line)` | Workspace-relative path + 1-based line of the call. |
| `freq` | Rough frequency tier per simulated day. `Σ` = once per agent per sim. `O` = per order/trade. `M` = per message. `W` = per wakeup. `T` = per market-data tick. |
| `payload shape` | Type and structure of the third arg (the "event"). `dict[...]` lists keys; `str` shows the format string; `scalar` is anything else. |
| `consumers` | Known reading sites in the workspace. |
| `proposed schema` | Free-text suggestion for what a future, structured form of this event might look like. Non-binding. |
| `disposition` | `keep` (the only allowed default) or `[REVIEW] <action>`. |

Frequency tiers are *order of magnitude* rules of thumb against
`rmsc04`-style sims; they are not measured. Use the `benchmarks/`
scripts for actual numbers.

## Known consumers (for the `consumers` column)

* `parse_logs_df` — `abides-core/abides_core/utils.py`. The single
  parsing primitive used by every downstream extractor. Reads
  `EventTime` + `EventType` from every entry; passes `event_dict` keys
  through to columns.
* `SimulationResult.order_logs` — `abides-markets/abides_markets/simulation/result.py`.
  Filters `parse_logs_df` output to `_ORDER_EVENT_TYPES`
  (`abides-markets/abides_markets/simulation/schemas.py:122-131`).
* `metrics._compute_per_agent_order_stats` —
  `abides-markets/abides_markets/simulation/metrics.py:1244-1290`.
  Branches on `EventType in {"ORDER_SUBMITTED","ORDER_EXECUTED","ORDER_CANCELLED"}`.
* `tests` — multiple, e.g. `test_market_boundaries.py:471` reads
  `ENDING_CASH`; `test_simulation.py:469-472` asserts the order
  lifecycle event set; `test_metrics.py` uses synthetic event rows.
* `abides-gym` — does not consume `EventType` strings; reads structured
  state from `FinancialGymAgent` raw_state.
* External users — by contract, can read `agent.log` directly. Treat
  every entry as part of the public vocabulary.

---

## A. Core (`abides-core`)

| event_type | producer (file:line) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `AGENT_TYPE` | [abides-core/abides_core/agent.py:101](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L101) | `Σ` | `str` (the agent's `type` attr) | `parse_logs_df` (becomes `ScalarEventValue` column); tests | `{"agent_type": str}` | keep | One row per agent at `kernel_starting`. Already redundant with the `agent_type` column `parse_logs_df` adds, but external readers may rely on the row. |

`Agent.report_metric()` is called in core only by tests (no
production producer in `abides-core/abides_core/`). Disposition: keep.

---

## B. Markets — `TradingAgent` lifecycle

All in [abides-markets/abides_markets/agents/trading_agent.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py).

| event_type | producer (line) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `STARTING_CASH` | [231](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L231) | `Σ` | `int` (cents) | tests; `parse_logs_df`; external | `{"starting_cash_cents": int}` | keep | Set in `kernel_starting`. |
| `FINAL_HOLDINGS` | [249](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L249) | `Σ` | `str` (formatted) | external | `{"holdings": dict[str,int]}` | `[REVIEW]` change to dict | Currently a pre-formatted display string; loses structure for downstream parsing. |
| `FINAL_CASH_POSITION` | [252](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L252) | `Σ` | `int` (cents) | external | `{"cash_cents": int}` | keep | |
| `ENDING_CASH` | [257](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L257) | `Σ` | `int` (mark-to-market cents) | `test_market_boundaries.py:471,501`; external | `{"mark_to_market_cents": int}` | keep | Tests assert by name. |
| `HOLDINGS_UPDATED` | [283, 1155](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L283) | `O` | `(symbol, delta_qty, qty_after, cash_after_cents)` (HOLDINGS_DELTA v2) | `reconstruct_holdings`; ParquetSink typed columns | typed delta tuple | Phase 2c: reshaped from dict snapshot to per-fill delta. Use `reconstruct_holdings` to fold into snapshot. |
| `MARK_TO_MARKET` | [1546](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1546) | `Σ` | `str` (formatted) | external | `{"symbol": str, "shares": int, "price": int, "value": int}` | `[REVIEW]` change to dict | Per-symbol breakdown of the mark-to-market computation; currently a display string. |
| `MARKED_TO_MARKET` | [1551](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1551) | `Σ` | `int` (cents) | external | `{"mark_to_market_cents": int}` | keep | Note vs. `MARK_TO_MARKET` (per-symbol) — same root, easily confused. |
| `MKT_CLOSED` | [1301](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1301) | `Σ` | `None` | external | unchanged (marker event) | keep | Empty event becomes `EmptyEvent: True` row. |

---

## C. Markets — `TradingAgent` order lifecycle

All in [abides-markets/abides_markets/agents/trading_agent.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py).
Payload is `order.to_dict()` unless noted.

> **Phase 2a (Order slotting).** `Order` and its subclasses are now
> slotted and expose `to_payload_tuple()` returning an 11-field tuple
> aligned with the `ORDER_EVENT` schema in
> `abides_core.event_payloads`. Publish sites still emit `to_dict()`
> for backwards compatibility with existing consumers; the tuple
> migration ships in a follow-up PR. Authors of new producers should
> prefer `to_payload_tuple()` so downstream `ParquetSink` can serialise
> typed Arrow columns without unpickling.

| event_type | producer (lines) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `ORDER_SUBMITTED` | [819, 882, 968](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L819) | `O` | `dict` (`order.to_dict()`) | `metrics:1249`; `OrderLogsSchema`; tests; external | unchanged | `[REVIEW]` dedupe call sites | Three identical call sites across `place_limit_order` / `place_market_order` / partial paths. |
| `ORDER_ACCEPTED` | [1173](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1173) | `O` | `dict` | `OrderLogsSchema`; external | unchanged | keep | |
| `ORDER_EXECUTED` | [1105](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1105) | `O` | `dict` (`order.to_dict()` — typically a partial fill) | `metrics:1266`; `OrderLogsSchema`; tests; external | unchanged | keep | One row per fill. Hot path on noisy sims. |
| `ORDER_CANCELLED` | [1191](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1191) | `O` | `dict` | `metrics:1285`; `OrderLogsSchema`; tests; external | unchanged | keep | |
| `PARTIAL_CANCELLED` | [1216](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1216) | `O` | `dict` | `OrderLogsSchema`; external | unchanged | keep | |
| `ORDER_MODIFIED` | [1247](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1247) | `O` | `dict` | `OrderLogsSchema`; external | unchanged | keep | |
| `ORDER_REPLACED` | [1274](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1274) | `O` | `dict` (old order) | `OrderLogsSchema`; external | `[REVIEW]` add `new_order` payload | Currently logs only `old_order`; lossy for downstream replay. |
| `CANCEL_SUBMITTED` | [994](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L994) | `O` | `dict` | external | unchanged | keep | Client-side counterpart to `ORDER_CANCELLED`. |
| `CANCEL_PARTIAL_ORDER` | [1032](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1032) | `O` | `dict` | external | `[REVIEW]` rename → `PARTIAL_CANCEL_SUBMITTED` for parity with `CANCEL_SUBMITTED` | Naming inconsistent with the rest of the lifecycle. |
| `MODIFY_ORDER` | [1050](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1050) | `O` | `dict` (new order) | external | `[REVIEW]` rename → `MODIFY_SUBMITTED` | Same inconsistency. |
| `REPLACE_ORDER` | [1089](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1089) | `O` | `dict` (new order) | external | `[REVIEW]` rename → `REPLACE_SUBMITTED` | Same inconsistency. |
| `STOP_ORDER_SUBMITTED` | [922](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L922) | `O` | `dict` | external | unchanged | keep | |
| `STOP_TRIGGERED` | [1317](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1317) | `O` | `dict` | external | unchanged | keep | |
| `CIRCUIT_BREAKER_TRIPPED` | [635, 654](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L635) | `Σ` (≤ once per agent) | `dict[{"reason": str, "loss"|"orders": int}]` | external | unchanged | keep | Two reasons (`max_drawdown`, `max_order_rate`) — already structured. |

---

## D. Markets — `TradingAgent` market-data echo

All in [abides-markets/abides_markets/agents/trading_agent.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py),
emitted from spread-response handler.

| event_type | producer (line) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `BID_DEPTH` | [1384](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1384) | `T` (per spread response) | `list[(price, qty)]` | external | `{"levels": list[[int,int]]}` | `[REVIEW]` consolidate with `ASK_DEPTH` and `IMBALANCE` into one `BOOK_SNAPSHOT` row | Three rows per spread response on a hot path. Major redundancy. |
| `ASK_DEPTH` | [1385](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1385) | `T` | `list[(price, qty)]` | external | as above | `[REVIEW]` see `BID_DEPTH` | |
| `IMBALANCE` | [1386](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L1386) | `T` | `[bid_qty_sum, ask_qty_sum]` | external | as above | `[REVIEW]` see `BID_DEPTH` | Computable from `BID_DEPTH` + `ASK_DEPTH`; pure redundancy. |

---

## E. Markets — `ExchangeAgent`

In [abides-markets/abides_markets/agents/exchange_agent.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/exchange_agent.py).

| event_type | producer (lines) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `<message.type()>` | [406, 414, 420](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/exchange_agent.py#L406), [993](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/exchange_agent.py#L993) | `M` (per inbound msg, gated on `exchange_log_orders`) | `dict` (`order.to_dict()`) or the message itself | external | `{"msg_type": str, "payload": dict}` | `[REVIEW]` settle dynamic vs. static `EventType` | The exchange writes `EventType = msg.type()` at runtime — the vocabulary is data-driven, which makes static enumeration impossible without crawling every `Message` subclass. Single largest source of vocabulary unpredictability. |
| `STOP_ORDER_ACCEPTED` | [844](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/exchange_agent.py#L844) | `O` (gated on `exchange_log_orders`) | `dict` (`order.to_dict()`) | external | `dict` (`order.to_dict()`) | harmonised — now in the `ORDER_EVENT` family | Pre-Phase 1 bundle this was the lone `str(order)` outlier. Switched to `to_dict()` for parity with `STOP_ORDER_SUBMITTED` and the rest of the lifecycle. |

---

## F. Markets — `OrderBook`

In [abides-markets/abides_markets/order_book.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py).
The order book holds a back-reference to its owning exchange and routes
events through `self.owner.logEvent()`.

| event_type | producer (line) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `BEST_BID` | [212](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py#L212) | `O` (after every order processed) | `str` `"{symbol},{price},{qty}"` | external | `{"symbol": str, "price": int, "qty": int}` | `[REVIEW]` change to dict | Hot path. CSV-in-string is parser-hostile and harder to schema. |
| `BEST_ASK` | [218](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py#L218) | `O` | `str` `"{symbol},{price},{qty}"` | external | as above | `[REVIEW]` see `BEST_BID` | |
| `LAST_TRADE` | [234](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py#L234) | per fill | `str` `"{trade_qty},${avg_price:0.4f}"` | external | `{"qty": int, "avg_price_cents": int}` | `[REVIEW]` change to dict; **drops dollar formatting** | Format includes `$` and 4 decimal places — pure display logic in the log row. |
| `<order.tag>_POST_ONLY` | [289](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py#L289) | `O` (post-only rejections only) | `dict[{"order_id": int}]` | external | unchanged | `[REVIEW]` settle dynamic prefix | Like the exchange's dynamic message-type case: the `EventType` string is built from `order.tag` at runtime. |

---

## G. Markets — Strategy & background agents

| event_type | producer (file:line) | freq | payload shape | consumers | proposed schema | disposition | notes |
|---|---|---|---|---|---|---|---|
| `FINAL_VALUATION` | [noise_agent.py:124](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/noise_agent.py#L124), [noise_agent.py:132](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/noise_agent.py#L132), [value_agent.py:120](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/value_agent.py#L120) | `Σ` | `int` or `float` | external | `{"surplus": float \| int}` | `[REVIEW]` unify payload type — currently `int` (cents) for one branch, `float` (frac) for the others | Three call sites disagree on the unit. Confusing for downstream comparison across agent types. |
| `AMM_FLATTEN` | [adaptive_market_maker_agent.py:567](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/market_makers/adaptive_market_maker_agent.py#L567) | `Σ` | `dict[{"symbol": str, "position_closed": int}]` | external | unchanged | keep | |
| `EXECUTION_SUMMARY` | [base_execution_agent.py:133](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/base_execution_agent.py#L133) | `Σ` | `dict[{"executed_quantity": int, "target_quantity": int, "remaining_quantity": int, "execution_rate": float}]` | external | unchanged | keep | |
| `SLICE_DECISION` | [base_execution_agent.py:230](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/base_execution_agent.py#L230) | `W` (per slice wakeup) | `dict[{"time": int, "order_size": int, "remaining_quantity": int, "direction": str}]` | external | unchanged | keep | |
| `POV_SUMMARY` | [pov_execution_agent.py:122](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/pov_execution_agent.py#L122) | `Σ` | `dict[{"effective_pov": float, "total_market_volume": int}]` | external | unchanged | keep | |

---

## H. `report_metric` producers

`Agent.report_metric()` writes to a separate dict-of-lists keyed by
metric name (not the per-agent `log` list). Producers are far rarer
than `logEvent`.

| metric | producer (file:line) | freq | payload | consumers | disposition | notes |
|---|---|---|---|---|---|---|
| `ending_value` | [trading_agent.py:264](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L264) | `Σ` | `int` (cents, `cash - starting_cash`) | `Kernel` summary log; tests | keep | Only production caller of `report_metric` in the shipped agents. |

`abides-gym` does not call `report_metric` directly.

---

## Aggregate observations

* **Conservative-default rule applied throughout.** No row is
  dispositioned `delete`, `rename`, or `merge` outright. All
  consolidation suggestions are `[REVIEW]` flags for a future phase
  with explicit scope.
* **Two structural patterns dominate the consolidation candidates:**
  1. **String payloads where dicts would do.** `BEST_BID`, `BEST_ASK`,
     `LAST_TRADE`, `FINAL_HOLDINGS`, `MARK_TO_MARKET`. All on hot
     paths; all force consumers to re-parse a CSV-in-string. Migration
     would be a typed-dict event with a deprecation cycle for the
     string form.
  2. **Multi-call redundancy at one logical site.** `BID_DEPTH` +
     `ASK_DEPTH` + `IMBALANCE` (three rows per spread response);
     `HOLDINGS_UPDATED` reshaped (Phase 2c) — call sites consolidated to two (first_wake + order_executed);
     `ORDER_SUBMITTED` (three call sites). Consolidation would route
     through a helper but keep the on-disk vocabulary stable.
* **Two dynamic-name producers** prevent fully static enumeration of
  the `EventType` set:
  * `ExchangeAgent` writes `msg.type()` at runtime.
  * `OrderBook` writes `f"{order.tag}_POST_ONLY"` at runtime.
  Any future schema-validation work needs to either enumerate every
  `Message` subclass or accept that the vocabulary is open.
* **Tests are a binding consumer.** `test_simulation.py:469-472`
  asserts the exact order-lifecycle event set; `test_market_boundaries.py`
  asserts `ENDING_CASH`. Renames would require coordinated test churn.

## OrderBook events on the EventBus

These six event types are published by `OrderBook` directly onto
`EventBus` (not via `Agent.logEvent`), with `agent_id=exchange.id` and
`agent_type="ExchangeAgent"` on the wire tuple.  Payloads are typed
`NamedTuple` subclasses defined in
[`abides_markets.book_events`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/book_events.py).
Trader attribution lives inside the payload (`agent_id`,
`oppos_agent_id`).  `symbol` is the **first** payload field so a single
history sink can demultiplex events from a multi-symbol exchange.

| event_type | producer | freq | payload `NamedTuple` | consumers | disposition |
|---|---|---|---|---|---|
| `LIMIT` | [order_book.py — `handle_limit_order`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py) | `O` | `LimitPayload(symbol, order_id, agent_id, side, quantity, price)` | `OrderBookHistoryMemorySink`; `runner._extract_*` via sink; `ExchangeAgent._handle_query_order_stream` | keep |
| `EXEC` | [order_book.py — match logic](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py) | `O` | `ExecPayload(symbol, order_id, agent_id, oppos_order_id, oppos_agent_id, side, quantity, price)` | `OrderBookHistoryMemorySink`; `runner._extract_liquidity` / `_extract_trades` (VWAP and TradeAttribution) | keep |
| `CANCEL` | [order_book.py — `cancel_order`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py) | `O` | `CancelPayload(symbol, order_id, tag, metadata)` | `OrderBookHistoryMemorySink`; `ExchangeAgent._handle_query_order_stream` | keep |
| `CANCEL_PARTIAL` | [order_book.py — partial cancel path](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py) | `O` | `CancelPartialPayload(symbol, order_id, quantity, tag, metadata)` | same as `CANCEL` | keep |
| `MODIFY` | [order_book.py — `modify_order`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py) | `O` | `ModifyPayload(symbol, order_id, new_side, new_quantity)` | same as `CANCEL` | keep |
| `REPLACE` | [order_book.py — `replace_order`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/order_book.py) | `O` | `ReplacePayload(symbol, old_order_id, new_order_id, quantity, price)` | same as `CANCEL` | keep |

Snapshot publishes use the separate `publish_book_snapshot` wire kind
(not `publish_event`) and have no `event_type` string; they carry
`(symbol, sim_time_ns, bids, asks, depth, seq)`.  See
[logging-architecture.md §5](logging-architecture.md#5-orderbook-capture-on-the-eventbus)
for the full producer / sink contract and the `book_capture` config
field.

## Cross-references

* [docs/reference/logging-architecture.md](logging-architecture.md) —
  the architectural shape of the log writer / parser pipeline this
  vocabulary feeds into.

# Changelog

All notable changes to ABIDES-NG (post-fork) are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries dated **2021-* and earlier** are inherited verbatim from the
upstream `abides-jpmc-public` project and are preserved for historical
reference.

---

## [Unreleased]

### Added
- **Phase 2b — payload schemas as a contract.**
  `abides_core.event_payloads.EVENT_TYPE_SCHEMA` is now a complete,
  frozen registry of every shipped event type. Each entry maps to a
  `PayloadSchema(name, version, fields)` whose `fields` arity
  determines the on-wire payload shape (0 → `EMPTY_PAYLOAD`, 1 → bare
  scalar, ≥ 2 → positional tuple). New schemas: `FILL_PNL`,
  `IMBALANCE_PAYLOAD`, `CIRCUIT_BREAKER`. New singleton
  `EMPTY_PAYLOAD` (`()`).
- **Phase 2c — typed Arrow columns in `ParquetSink` and `HOLDINGS_UPDATED`
  delta reshape.**
  - `ParquetSink` now builds one typed Arrow schema per registered
    `PayloadSchema`, so each event-type file carries one named, typed
    column per field (e.g. `order_id: int64`, `symbol: string`,
    `delta_qty: int64`). No unpickling step is needed when reading
    typed event buckets. `BUS_FORMAT_VERSION` bumped to `"2"`;
    existing v1 files are rejected at read time.
  - `HOLDINGS_UPDATED` reshaped from a deep-copied `dict[str,int]`
    snapshot to the lean `HOLDINGS_DELTA` v2 schema: positional tuple
    `(symbol, delta_qty, qty_after, cash_after_cents)`. `delta_qty` is
    signed (positive for buys, negative for sells). At `first_wake` one
    row per held symbol is emitted with `delta_qty == qty_after`, making
    the stream self-contained.
  - New `abides_markets.utils.reconstruct_holdings(rows)` folds an
    iterable of `HOLDINGS_DELTA` tuples (or a DataFrame) back into a
    `dict[str, int]` holdings snapshot using last-write-wins semantics.
  - Removed `deepcopy_event=True` from all `HOLDINGS_UPDATED` call
    sites (tuples are immutable).
  - Removed three stale call sites that emitted `HOLDINGS_UPDATED` on
    order-state-refresh paths that do not mutate `self.holdings`
    (`order_partial_cancelled`, `order_modified`, `order_replaced`).
- `Side.legacy_str()` and `TimeInForce.legacy_str()` — explicit
  human-readable accessors retained for reporting; the enum values
  themselves are now `IntEnum` and cross the wire as integers.
- `InMemorySink.columns` (zero-copy access to the per-event-type
  columnar storage) and `InMemorySink.bucket_schema(event_type)`
  (returns the `PayloadSchema` chosen for a given bucket, or `None`).
- `abides-core/tests/test_event_payload_schema.py` — build-time AST
  audit that walks every in-tree `.py` file and fails if a publisher
  call site uses an unregistered string-literal `event_type` or an
  f-string / `str(x)` payload literal.
- Three-valued `book_capture` field on `ExchangeAgent` (`"off"` / `"l1"` /
  `"l2"`) — fine-grained control over order-book snapshot retention.
- Order-book capture flows through the `EventBus`: new
  `OrderBookSnapshotMemorySink` and `OrderBookHistoryMemorySink`, one
  pair per symbol. Typed `NamedTuple` payloads for `LIMIT`, `EXEC`,
  `CANCEL`, `CANCEL_PARTIAL`, `MODIFY`, `REPLACE`.
- Publisher-side L1 deduplication for `book_capture="l1"`.
- Optional `ParquetSink` (`abides_core.parquet_sink`) for columnar
  persistence of bus emissions to per-bucket Parquet files, with
  atomic writes via a `.partial/` staging directory, optional
  row-count checkpoint rotation, schema/bus-format version metadata,
  and a `__generic__` fallback for unknown event types. Install with
  `pip install 'abides-ng[parquet]'`. Companion `read_parquet_logs`
  reader returns a `dict[kind, dict[key, DataFrame]]`, and
  `unpickle_payloads` materializes pickled payload columns.
- `Order`, `LimitOrder`, `MarketOrder` and `StopOrder` now use
  `__slots__` — instances drop the per-instance `__dict__`, saving
  roughly 280 bytes per order on CPython 3.12.
- `Order.to_payload_tuple()` (new abstract method) returns an
  11-field positional tuple aligned with the `ORDER_EVENT` schema in
  `abides_core.event_payloads`, with an `order_kind` class
  discriminator (`"LIMIT"` / `"MARKET"` / `"STOP"`) per subclass.

### Changed
- **Breaking — order-lifecycle payloads.** Every event in the
  `ORDER_EVENT` family (`ORDER_SUBMITTED`, `ORDER_ACCEPTED`,
  `ORDER_EXECUTED`, `ORDER_CANCELLED`, `PARTIAL_CANCELLED`,
  `ORDER_MODIFIED`, `ORDER_REPLACED`, `CANCEL_SUBMITTED`,
  `CANCEL_PARTIAL_ORDER`, `MODIFY_ORDER`, `REPLACE_ORDER`,
  `STOP_ORDER_SUBMITTED`, `STOP_TRIGGERED`, `STOP_ORDER_ACCEPTED`,
  plus the dynamic `<message.type()>` echoes from `ExchangeAgent`)
  now ships the 11-field `ORDER_EVENT` positional tuple produced by
  `order.to_payload_tuple()` instead of the legacy `order.to_dict()`
  dict. `parse_logs_df()` projects the tuple back into the same
  named columns existing consumers expect.
- **Breaking — `Side` and `TimeInForce` are `IntEnum`.** Wire payloads
  now carry the integer enum value; use `Side.legacy_str()` /
  `TimeInForce.legacy_str()` for human-readable rendering.
- **Breaking — `BEST_BID` / `BEST_ASK`** now ship
  `(symbol, price, qty)`; **`LAST_TRADE`** now ships
  `(symbol, avg_price_cents, qty)`. The CSV-in-string payloads are
  gone.
- **Breaking — `CIRCUIT_BREAKER_TRIPPED`** now ships
  `(reason, value)`; the previously asymmetric `loss` / `orders`
  integer is harmonised under one field.
- **Breaking — `MARK_TO_MARKET`** now ships a bare integer (cents)
  under the `CASH` schema (was a free-form `str` under the `SUMMARY`
  schema). The per-symbol breakdown that used to share the name is
  now a `logger.debug` line, not a logged event.
- **Breaking — `EXECUTION_SUMMARY`, `SLICE_DECISION`, `POV_SUMMARY`,
  `AMM_FLATTEN`, `FILL_PNL`, `MKT_CLOSED`** now ship the positional
  tuple / `EMPTY_PAYLOAD` form mandated by their registered schemas.
- **`ExchangeAgent` receipt echoes** are now allowlisted to
  `QueryMsg`, `MarketHoursRequestMsg`, `MarketClosePriceRequestMsg`
  and `MarketDataSubReqMsg`, each logged with an `EMPTY_PAYLOAD`
  under the message class name. Any other message type is dropped
  with a stdlib-logger warning rather than leaking the raw
  `Message` instance onto the bus.
- **`InMemorySink`** rebuilt around a per-event-type columnar layout
  (`_cols: dict[event_type, dict[column, list]]`). Each bucket
  carries the four common wire columns (`agent_id`, `agent_type`,
  `sim_time_ns`, `seq`) plus one column per registered schema
  field; payload shape is validated before any column is touched
  (mismatches are diverted to a per-type
  `"<event_type>::generic"` fallback bucket). `events`,
  `agent_log()` and `to_dataframe()` reconstruct on demand and
  remain fully backwards-compatible.
- **`parse_logs_df`** is now schema-aware: registered payloads are
  projected into named columns via `EVENT_TYPE_SCHEMA`; arity 0 →
  `{"EmptyEvent": True}`, arity 1 → `{fields[0]: value}`, arity ≥ 2
  → `dict(zip(fields, payload, strict=True))`. Dict payloads pass
  through unchanged.
- `EventBus` is now the single dispatch hub for agent events, metrics,
  and order-book snapshots. `Agent.logEvent()` and `Agent.report_metric()`
  publish through the bus; `InMemorySink` is the default sink.
- `Kernel` accepts injectable `LogWriter` and `event_sinks`; `log_root`
  is a first-class kwarg.
- `parse_logs_df` rebuilt around a single `pd.DataFrame.from_records`
  call.
- `Order.__eq__` now compares the MRO-walked slot tuple instead of
  `self.__dict__` (slotted instances expose no instance dict). The
  semantics are preserved: same concrete type, same data → equal.
  `Order` instances remain unhashable (`__hash__ = None`).
- `Order.to_dict()` no longer round-trips through `deepcopy(self).__dict__`
  — it now reads slot values directly. The returned key set and
  shape are preserved; the change drops an unnecessary defensive copy
  (sinks must not mutate received payloads).
- `Kernel.write_summary_log()` now honours `skip_log`: when set, the
  method early-returns instead of building the summary `DataFrame`.
  The injected `_log_writer` was already a `NoOpLogWriter` in that
  mode, so on-disk behaviour is unchanged — the wasted per-terminate
  `pd.DataFrame` allocation is dropped.
- `STOP_ORDER_ACCEPTED` now emits the `ORDER_EVENT` tuple, joining
  the rest of the order lifecycle. (Earlier in this release it had
  already been changed from `str(order)` to `order.to_dict()`; the
  Phase 2b tuple migration supersedes that intermediate form.)

### Deprecated
- `Order.to_dict()` is retained as a deprecation-window wrapper that
  rebuilds the legacy dict from the slot values; it will be removed
  in the Phase 5+2 cleanup. New producers must call
  `order.to_payload_tuple()` (enforced by the AST audit).
- `OrderBook.book_log2` and `OrderBook.history` — read from the
  corresponding sink instead.
- Direct `agent.log` access — use
  `kernel.event_bus.in_memory_sink.agent_log(agent_id)`.
- Legacy bzip2-pickle log path: `BZ2PickleLogWriter` and
  `BZ2PickleSink` now emit a `DeprecationWarning` (once per process)
  on construction. Migrate to `ParquetSink`
  (`abides_core.parquet_sink.ParquetSink`) or another EventBus-
  registered columnar sink.
- `Agent.logEvent(append_summary_log=True)` and
  `Kernel.append_summary_log` now emit a `DeprecationWarning` (once
  per process). The `summary_log` path will be removed; register a
  `MetricsObserverSink` (or any custom `EventSink`) on
  `Kernel.event_bus` instead. See
  `docs/active-plans/event-logging-refactor-plan.md` §5 for the
  deprecation timeline.

### Fixed
- **`ExchangeAgent` raw-`Message` leak.** The previous catch-all
  `self.logEvent(message.type(), message)` could push a raw
  `Message` instance onto the bus (causing pickle / Parquet
  serialisation failures downstream). The new isinstance allowlist
  forces an `EMPTY_PAYLOAD` for query / subscription receipts and
  drops anything else with a logger warning.
- Repeated `OrderBook.history` / `book_log2` reads no longer emit
  spurious deprecation warnings during normal operation.

---

## [2.6.0] — 2026-05

### Changed
- **Breaking:** distribution renamed from `abides` to `abides-ng` on
  PyPI. Import paths (`abides_core`, `abides_markets`, `abides_gym`)
  are unchanged.
- Documentation site moved to MkDocs Material; published from `main`
  via GitHub Actions.

### Fixed
- Numerous correctness fixes across the agent suite — see git history
  for details.

---

## [2.5.8] — 2026-04

### Added
- `SimulationResult.get_agents_by_category()` helper.

## [2.5.7] — 2026-04

### Added
- Per-agent execution-quality metrics surfaced on
  `compute_rich_metrics()`.

## [2.5.6] — 2026-04

### Added
- VPIN (Easley et al. 2012) added to the microstructure metrics
  surface.

### Fixed
- Edge cases in trade-attribution bucketing under thin liquidity.

## [2.5.4] — 2026-04

### Added
- MiFID II RTS 9 market-wide order-to-trade ratio
  (`MicrostructureMetrics.market_ott_ratio`).
- Spread-resilience indicator (Foucault, Pagano & Röell 2013).

### Fixed
- Adverse-selection window alignment for sub-second horizons.

## [2.5.3] — 2026-04

### Added
- `ResultProfile.FULL` exposes raw event logs (`SimulationResult.logs`)
  as a parsed DataFrame.
- Declarative config templates for thin-liquidity and volatile-regime
  scenarios.

### Changed
- Config validation messages now point at the offending field path.

## [2.5.2] — 2026-04

### Added
- `compute_rich_metrics(include_fills=True)` returns per-fill
  attribution and VWAP slippage.

### Fixed
- `BaseAgentConfig._prepare_constructor_kwargs()` propagates non-
  serializable strategy instances correctly.

## [2.5.1] — 2026-03

### Changed
- Lower per-message overhead in the kernel hot path.

### Fixed
- Several test-only fixtures that leaked state between runs.

## [2.5.0] — 2026-03

### Added
- **Execution agents:** TWAP, VWAP, and POV implementations with
  declarative scheduling and order-rate caps.
- **Time-in-force order types:** `IOC`, `FOK`, `DAY`.
- **Exchange-side stop orders** with deterministic trigger semantics.
- **Market-maker enhancements:** configurable inventory bands and
  quote-skew under inventory pressure.
- **Trade attribution** exposed on `SimulationResult.markets`.

## [2.4.0] — 2026-03

### Changed
- **Breaking:** RNG hierarchy reworked to SHA-256 identity hashing —
  adding or removing an agent no longer shifts the random draws of
  any other agent. Per-worker seeds in `run_batch` are now
  composition-invariant.

## [2.3.0] — 2026-03

### Added
- Human-readable units in declarative config (`"100ms"`, `"30s"`,
  `"$10.00"`).

## [2.2.1] — 2026-03

### Changed
- Internal code-quality pass; no behaviour changes.

## [2.2.0] — 2026-03

### Added
- **Risk controls:** declarative position limits, drawdown kill-switch,
  and order-rate caps enforced at order entry.
- **Oracle redesign:** every `MarketConfig` must explicitly choose an
  oracle (or `oracle: null`). `ValueAgent` auto-inherits oracle
  parameters. External-data oracles are injected via
  `builder.oracle_instance(...)`.
- `compute_rich_metrics()` first release: Sharpe, max drawdown, mean
  spread, LOB imbalance.

### Changed
- All constructor-side defaults aligned with their config-side
  counterparts.
- Order-management hot path vectorised.

### Fixed
- Off-by-one in the closing-cross fallback when no oracle is present.

## [2.1.0] — 2026-03

### Changed
- Code-quality pass; type annotations modernised to `X | None` syntax
  (Python 3.12+).

## [2.0.0] — 2026-03

### Fixed
- **Breaking:** several long-standing correctness bugs in
  `MeanReversionAgent`, `ValueAgent`, and `MeanRevertingOracle`.
  Results from earlier versions are not directly comparable.

## [1.3.0] — 2026-03

### Added
- **Declarative configuration system:** `SimulationConfig`,
  `SimulationBuilder`, agent registry, YAML / JSON serialisation,
  composable templates.
- `run_simulation()` and `SimulationResult` as the recommended entry
  points.

### Changed
- **Breaking:** project renamed in source from upstream layout.

## [1.2.0] — 2026-03

### Changed
- Hot-path performance improvements in message dispatch and order-book
  matching.

### Fixed
- Multiple correctness fixes carried over from the upstream backlog.

## [1.1.0] — 2026-01

First release after forking the archived `abides-jpmc-public` project.

### Added
- `POVExecutionAgent` implementation (referenced in upstream RMSC03
  but never shipped).

### Changed
- Modernised dependency stack: Python 3.12+, NumPy 2.x, Pandas 2.x,
  Gymnasium (replaces deprecated Gym), Ray 2.40+, SciPy 1.14+,
  matplotlib 3.9+. `pomegranate` removed.

### Fixed
- `version_testing` regression suite restored; hard-coded commit
  comparisons removed.

---

## 2021-10-15 Release

### New Features
- WandB + rllib custom metrics + background V2 + autocalib (PR #86)

### Other Changes
- Code cleanup for open source release (PR #87, #88, #90)
- Separation Gym core into markets and true gym core (PR #91)


## 2021-09-28 Release

### New Features
- Order book state subscriptions and alerts (PR #73)
- ABIDES-gym (PRs #77, #79, #82)
- Event data subscriptions (PR #81)
- Background Agents v2 + wandB scripts (PR #85)

### Other Changes
- Optimise agent event log data structures for memory use (PR #74)
- Feature marketreplay realdata2 (PR #75)
- Replace use of pd.Timestamp with raw ints (PR #76)
- Simplify kernel initialisation (PR #80)
- Add message batches (PR #83)

### Bugs Fixed
- Fix debug message for modify order msg (PR #72)
- Fix Order Book Imbalance subscription (PR #78)


## 2021-07-27 Release

### New Features
- Add price to comply order types (PR #64)
- Add insert by ID option for limit orders (PR #68)

### Other Changes
- Improve abides-cmd and config layout (PR #62)

### Bugs Fixed
- Fix the place order of MM to not place order of size 0 when backstop qty = 0 (PR #63)
- Fix attempts to cancel MarketOrders (PR #65)
- Remove placing orders when size is 0 (PR #66)
- Add test for negative price on limit orders (PR #67)
- Fix regression tests (PR #69)
- Fix r_bar types (PR #70)
- Fix end of day mark to market (PR #71)


## 2021-06-29 Release

### New Features
- Add hidden orders to order book (PR #56)
- Use built-in Python logging library (PR #57)

### Other Changes
- Reorganise directory layout (PR #54)
- Add initial pre-commit hooks and RMSC unit test (PR #58)
- Update generated documentation to work with refactored code layout (PR #59)
- Replace is_buy_order flag with Side.BID or Side.ASK enum (PR #60)

### Bugs Fixed
- Correcting markToMarket function to multiply by shares (PR #61)


## 2021-06-15 Release

### New Features
- Add transacted volume and L1 and L3 data subscriptions (PR #42)
- Change message types to use dataclasses (PR #45)
- Add replaceOrder command to OrderBook (PR #48)

### Other Changes
- Refactor subscription message and data classes (PR #42)
- Simplify market order handling code by removing limit order creation (PR #43)
- Use a flat data structure to store order history (PR #44)
- Simplify handleLimitOrder (PR #46)
- Refining message stream logging and orderbook log2 (PR #47)
- Add more unit tests for OrderBook (PR #49)

### Bugs Fixed
- Fix NoiseAgent and Value agent random seed sources (PR #50)
- Fix various Message class issues (PR #51)


## 2021-06-01 Release

### New Features
- Initial OrderBook unit tests (PR #39)
- New order book data getting methods (L1, L3 data) (PR #40)

### Other Changes
- OrderBook history and tracking now optional (PR #27)
- Removed unused ExchangeAgent code (PR #30)
- Reduce number of Python deepcopies (PR #32)
- General tidy of Order classes (PR #38)
- Add warning if invalid arguments passed to abides_cmd (PR #41)

### Bugs Fixed
- Fix RMSC03 spec documentation (PR #35)
- Fix version testing script timer (PR #36)


## 2021-05-18 Release

### New Features
- Automatically generated documentation using Sphinx-Doc (PR #18)
- Developer guide in documentation with best practices (PR #18)

### Other Changes
- Improved documentation strings in code (PR #18)
- Type annotations added for most files (PR #18)
- Faster deepcopy of orders (PR #21)
- Simplified order ID generation (PR #25)
- Agent class 'type', 'name' and 'RandomState' parameters now optional (PR #26)

### Bugs Fixed
- Correction of type errors in Noise agent (PR #23)
- Noise calculation error fix (PR #17)

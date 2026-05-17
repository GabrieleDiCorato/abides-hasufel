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

### Changed
- `EventBus` is now the single dispatch hub for agent events, metrics,
  and order-book snapshots. `Agent.logEvent()` and `Agent.report_metric()`
  publish through the bus; `InMemorySink` is the default sink.
- `Kernel` accepts injectable `LogWriter` and `event_sinks`; `log_root`
  is a first-class kwarg.
- `parse_logs_df` rebuilt around a single `pd.DataFrame.from_records`
  call.

### Deprecated
- `OrderBook.book_log2` and `OrderBook.history` — read from the
  corresponding sink instead.
- Direct `agent.log` access — use
  `kernel.event_bus.in_memory_sink.agent_log(agent_id)`.

### Fixed
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

# Phase 3a — Move OrderBook capture onto the EventBus

> Ephemeral coordination plan. Long-lived design lives in
> `docs/project/event-logging-refactor-plan.md`. Delete this file when
> Stage 4 (docs update) is done.

## Decisions locked in advance (from user Q&A)

| # | Decision | Choice |
|---|----------|--------|
| Q1 | Scope of this PR | **Full Phase 3a in one PR** — snapshots **and** history, plus runner rewire, deprecated cached properties, and tests. |
| Q2 | Snapshot capture controls | **New config field `book_capture: "off" \| "l1" \| "l2"`** on `ExchangeAgent` / market config. Controls both *what the publisher computes* (skip the L2 walk when `"l1"`) and *what the sink stores*. Replaces the per-instance `book_logging` flag (kept as a deprecation shim). |
| Q3 | Agent attribution for book-emitted events | **Exchange-as-producer**: `agent_id=exchange.id`, `agent_type="ExchangeAgent"`. Trader ids live inside the payload (`order_id`, `agent_id`, plus `oppos_*` for `EXEC`). |

## Decisions I am making in the plan (challenge them in review)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | History payload shape | **Migrate to per-type `NamedTuple` payloads now** (user override of original plan). Slotted `Order` and any further allocation reductions stay in Phase 4. Six NamedTuple classes — see "History payload schema" below. The `time` and `type` keys are dropped from the payload (they are carried by `sim_time_ns` and `event_type`); the deprecated `history` property re-materializes them for byte-equivalence. |
| D2 | Event-type namespacing | **Bare strings** (`"LIMIT"`, `"EXEC"`, `"CANCEL"`, `"CANCEL_PARTIAL"`, `"MODIFY"`, `"REPLACE"`). Matches today's `history["type"]` — zero migration for consumers. Documented in `docs/reference/event-vocabulary.md`. |
| D3 | Sink granularity | **Per-symbol** `OrderBookSnapshotMemorySink` and **per-symbol** `OrderBookHistoryMemorySink`. ExchangeAgent owns one `order_books` dict keyed by symbol; sinks mirror it. Cleaner extractor code than one shared sink with internal symbol bucketing. |
| D4 | Publisher-side L1 cache | **Include in this PR.** ~3 lines on `OrderBook`: cache `(last_bid_top, last_ask_top)`; when `book_capture == "l1"`, short-circuit the publish if the top has not changed. This is where the headline perf win comes from. |
| D5 | Where sinks get auto-registered | The **runner** (`abides_markets/simulation/runner.py` and/or the compile step) reads the exchange's `book_capture` setting and constructs the appropriate sinks, then passes them through `Kernel(event_sinks=[...])`. ExchangeAgent itself only publishes — it does not register sinks. `event_sinks` is added to the `kernel_keys` whitelist in `abides.py:_kernel_from_runtime`. |
| D6 | Default for `book_capture` | **`"l2"`** to preserve byte-equivalent behavior for users not opting in. The perf-first `"l1"` is one config field away. Today's `book_logging=True` users see no change; `book_logging=False` users see no change. Documented in CHANGELOG. |
| D7 | Deprecated cached properties | `OrderBook.book_log2` and `OrderBook.history` become cached `@property` methods that materialize a tuple-of-frozen-dicts from the sinks on first access. They emit `DeprecationWarning` on first access (per-instance flag to avoid spamming). Removed in a later phase per the long-lived plan. |
| D8 | What happens when `book_capture == "off"` | OrderBook does not publish at all (publisher-side guard). Sinks are not registered. The deprecated properties return empty tuples. |

## Architectural impact

- **Producer (OrderBook)**: 6 `append_book_log2()` call sites and 7 `history.append(dict(...))` call sites are replaced with `bus.publish_book_snapshot(...)` and `bus.publish_event(exchange.id, "ExchangeAgent", t, type_str, payload_dict)` respectively. The per-instance `self.book_log2: list` and `self.history: list` *attributes* are removed from `__init__`; the deprecated read paths are reimplemented as cached properties that read from the sinks via the kernel.
- **Bus**: no schema changes. The existing `publish_book_snapshot(symbol, sim_time_ns, bids, asks, depth)` and `publish_event(agent_id, agent_type, sim_time_ns, event_type, payload)` signatures are used as-is. The bus's `start()`-time no-op rebind already handles the "no accepting sink" case for free, so we can drop the publisher-side `if owner.book_logging:` guard except for the explicit `book_capture == "off"` short-circuit (which is faster than going through the bus's no-op stub).
- **Sinks**: two new `EventSink` implementations in `abides-core/abides_core/event_sinks.py`:
  - `OrderBookSnapshotMemorySink(symbol: str, depth: int)` — `accept_book_snapshots = True`. Filters incoming snapshots by `symbol`. Stores parallel column arrays: `times: list[int]`, `bids: list[tuple[tuple[int,int],...]]`, `asks: list[tuple[tuple[int,int],...]]`. Exposes `as_book_log2() -> tuple[dict, ...]` for the deprecated property.
  - `OrderBookHistoryMemorySink(symbol: str)` — `accept_events = True`. Filters by `event_type in _BOOK_EVENT_TYPES`. Stores `list[dict]` (the payload, with `time` re-derived from `sim_time_ns` if needed for parity).
- **Runner extractors** (`abides-markets/abides_markets/simulation/runner.py`): `_extract_l1_close`, `_extract_l1_series`, `_extract_l2_series` read from the snapshot sink instead of `exchange.order_books[symbol].book_log2`. `_extract_liquidity` and `_extract_trades` read EXEC entries from the history sink instead of `order_book.history`. The kernel's `event_sinks` list is exposed via `KernelRunResult` or accessed through the kernel reference the runner already holds — TBD in implementation, see Step 7.
- **ExchangeAgent**: `__init__` gains `book_capture: Literal["off","l1","l2"] | None = None`. When `None`, fall back to legacy `book_logging` mapping (`True → "l2"`, `False → "off"`) with no warning. When both are passed, warn and let `book_capture` win.
- **Public API**: `compile()` / `SimulationBuilder` propagate the new `book_capture` field into the compiled runtime. `_kernel_from_runtime` learns to accept `event_sinks`.

## History payload schema (D1 — locked)

All `time` and `type` fields are dropped (carried by `sim_time_ns` and `event_type`).  All payloads are `typing.NamedTuple` subclasses defined in a new module `abides-markets/abides_markets/book_events.py` (re-exported from `abides_markets`).

```python
class ExecPayload(NamedTuple):
    order_id: int
    agent_id: int
    oppos_order_id: int
    oppos_agent_id: int
    side: str               # "BUY" or "SELL" (point of view of the passive order being executed)
    quantity: int
    price: int

class LimitPayload(NamedTuple):
    order_id: int
    agent_id: int
    side: str
    quantity: int
    price: int

class CancelPayload(NamedTuple):
    order_id: int
    tag: str | None
    metadata: dict | None    # only set when tag == "auctionFill"

class ModifyPayload(NamedTuple):
    order_id: int
    new_side: str
    new_quantity: int

class CancelPartialPayload(NamedTuple):
    order_id: int
    quantity: int
    tag: str | None
    metadata: dict | None

class ReplacePayload(NamedTuple):
    old_order_id: int
    new_order_id: int
    quantity: int
    price: int
```

Event-type → payload-class mapping is exposed as a module-level constant `BOOK_EVENT_PAYLOAD_CLASSES: dict[str, type[NamedTuple]]` so the history sink can validate incoming payloads and the deprecated property can re-construct dicts.

## Reproducibility contract

With `book_capture="l2"` and a fixed seed, `SimulationResult.l1_close`, `l1_series`, `l2_series`, `trades`, and `liquidity` must be **byte-identical** to a baseline produced before this PR (after canonical sort on `(time, ...)`). This is asserted in a dedicated regression test.

With `book_capture="l1"`, only `l1_close` and `l1_series` are byte-identical. `l2_series` and book-walk-derived metrics are disabled / empty (this is the documented contract).

## Step plan

> Each step is small enough to run pre-commit + targeted tests on. Group steps into commits as noted.

### Step 1 — Add `book_capture` to `ExchangeAgent` (no behavior change yet)
- Add the `book_capture` constructor arg with the back-compat mapping from `book_logging`.
- Add `self.book_capture: Literal["off","l1","l2"]` resolved attribute.
- Leave the existing `if self.owner.book_logging:` guards in OrderBook untouched.
- Update `BaseAgentConfig`-derived ExchangeAgent config (search `abides-markets/abides_markets/agents/config/` for the right module) to expose the new field.
- **Commit:** "Added book_capture config field to ExchangeAgent with backward-compatible mapping from book_logging"

### Step 2 — Implement the two new sinks
- Add `OrderBookSnapshotMemorySink` and `OrderBookHistoryMemorySink` to `event_sinks.py`.
- Each accepts a `symbol` filter. Snapshot sink takes `depth: int` (just for documentation / future schema validation).
- Both implement the `EventSink` Protocol with empty `flush()` and no-op `on_simulation_start`/`on_simulation_end`.
- Snapshot sink stores raw column arrays; expose `as_book_log2()` returning the old shape `tuple[dict, ...]` with `{"QuoteTime", "bids", "asks"}`.
- History sink stores the payload dicts; expose `entries() -> tuple[dict, ...]`.
- **Tests:** unit tests for both sinks under `abides-core/tests/test_event_sinks.py`: drive synthetic publish calls, assert the materialized old-shape output matches.
- **Commit:** "Added OrderBookSnapshotMemorySink and OrderBookHistoryMemorySink"

### Step 3 — Plumb `event_sinks` through `_kernel_from_runtime`
- Add `"event_sinks"` to `kernel_keys` in `abides-core/abides_core/abides.py`.
- Add an `event_sinks` field to the compiled runtime in `abides-markets/abides_markets/simulation/` (search for the compile path; likely in a `compiler.py` or `build.py`).
- **Test:** small test that passes `event_sinks=[InMemorySink()]` through `run()` and confirms the sink is registered.
- **Commit:** "Plumbed event_sinks through compile/runtime/kernel"

### Step 4 — Auto-register book sinks from the runner
- In the compile / runner path, after the kernel is built, inspect the ExchangeAgent's `book_capture`. For each symbol, construct one snapshot sink + one history sink. Hand them to `Kernel(event_sinks=[...])`.
- Skip registration when `book_capture == "off"`.
- Preserve user-supplied `event_sinks` if any; the book sinks are *appended*, not *replaced*.
- Store handles to the registered sinks somewhere the extractors can find them (kernel attribute `kernel.book_snapshot_sinks: dict[str, OrderBookSnapshotMemorySink]` and `kernel.book_history_sinks: dict[str, OrderBookHistoryMemorySink]`, populated by the runner before `kernel.run()`).
- **Commit:** "Auto-registered per-symbol book sinks based on exchange book_capture"

### Step 5 — Capture frozen baseline + add regression test scaffold
- Run a small fixed-seed simulation **against the unmodified publish path** (i.e. before any OrderBook migration) and pickle the relevant `SimulationResult` fields (`l1_close`, `l1_series`, `l2_series`, `trades`, `liquidity`) to `abides-markets/tests/data/book_capture_baseline_l2.pkl`.
- Add `abides-markets/tests/test_book_capture_reproducibility.py` with two tests:
  - `test_l2_byte_equivalent`: runs the same fixed-seed simulation with `book_capture="l2"` and asserts equality against the baseline pickle (after canonical sort).
  - `test_l1_subset_of_l2`: runs with `book_capture="l1"` and asserts `l1_close` + `l1_series` equal the baseline subset, and that `l2_series` is empty.
- **Both tests are expected to fail at this point** — they will pass after Steps 6 + 7. The pickled baseline is the gate.
- **Commit:** "Captured book_capture baseline pickle and added reproducibility regression tests"

### Step 6 — Migrate OrderBook publish sites
- Add the publisher-side L1 cache `(self._last_bid_top, self._last_ask_top)`. Initialize to `(None, None)`.
- Replace all 6 `if self.owner.book_logging: self.append_book_log2()` blocks with a single helper `self._publish_snapshot(self.owner.current_time)`:
  - if `book_capture == "off"`: return
  - if `book_capture == "l1"`: read top of book; if equal to cached tops, return; else update cache and call `bus.publish_book_snapshot(symbol, t, ((bid_p, bid_q),), ((ask_p, ask_q),), 1)`
  - if `book_capture == "l2"`: call `bus.publish_book_snapshot(symbol, t, get_l2_bid_data(depth), get_l2_ask_data(depth), depth)`
- Replace all 7 `self.history.append(dict(...))` sites with `bus.publish_event(exchange.id, "ExchangeAgent", current_time, type_str, payload_namedtuple)` using the `book_events.py` NamedTuple classes (D1 schema above).
- Remove `self.book_log2 = []` and `self.history = []` from `OrderBook.__init__` (they become properties — see Step 7).
- **Run** the regression tests from Step 5 — `test_l2_byte_equivalent` should now pass. (`test_l1_subset_of_l2` may still fail until Step 8 wires the runner.)
- **Commit:** "Migrated OrderBook capture sites to publish on the EventBus with typed payloads"

### Step 7 — Add deprecated `book_log2` and `history` cached properties
- `@property def book_log2(self)`: lazily look up the kernel's `book_snapshot_sinks[self.symbol]` and call its `as_book_log2()`. Cache the result on the instance. Emit `DeprecationWarning` once.
- `@property def history(self)`: lazily look up `book_history_sinks[self.symbol].entries()`, reconstruct legacy dict shape by prepending `time` (from `sim_time_ns`) and `type` (from `event_type`) plus the NamedTuple's `_asdict()`. Cache + warn once.
- **Tests:** assert the deprecated properties return byte-equivalent output to a baseline; assert the warning fires exactly once per instance.
- **Commit:** "Added deprecated OrderBook.book_log2 and OrderBook.history cached properties"

### Step 8 — Rewire runner extractors
- `_extract_l1_close`, `_extract_l1_series`, `_extract_l2_series`: read from `kernel.book_snapshot_sinks[symbol]` directly (preferred), not via the deprecated property.
- `_extract_liquidity` and `_extract_trades` (VWAP / EXEC scan): read from `kernel.book_history_sinks[symbol]`. Iterate over NamedTuple payloads filtered by `event_type == "EXEC"`.
- Verify the existing `_extract_liquidity` `last_trade` fallback still works (it reads `order_book.last_trade`, which is not changed).
- **Run** `test_l1_subset_of_l2` from Step 5 — it should now pass.
- **Commit:** "Rewired runner extractors to read from EventBus sinks"

### Step 9 — Stage 4: docs & CHANGELOG
- Update `docs/reference/logging-architecture.md` (and create `docs/reference/event-vocabulary.md` if it doesn't exist yet) to document:
  - The `book_capture` config field and its three values.
  - The new per-symbol book sinks and how to register custom ones.
  - The bare-string event vocabulary for book events.
  - The deprecation timeline for `OrderBook.book_log2` / `.history`.
- Update `CHANGELOG.md` under an `[Unreleased]` section: new field, new sinks, deprecation notices, behavior contract.
- Update `docs/reference/llm-gotchas.md` if anything in the publish-vs-sync space is worth flagging.
- Delete this plan file (`docs/active-plans/phase3a-orderbook-on-bus.md`).
- **Commit:** "Documented book_capture and book sinks; deprecated OrderBook.book_log2/.history"

## Out of scope for this PR (Phase 4+ territory)

- Slotted `Order` and any further allocation reductions on the order path.
- Removing the deprecated `book_log2` / `history` properties (one release cycle of deprecation first).
- pyarrow-backed sinks.
- Cross-process / shared-memory sinks.
- Any change to `metric_trackers`, `MetricsObserverSink`, or non-book event flows.

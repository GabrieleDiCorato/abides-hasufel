# Event Logging — Refactoring & Improvement Plan

**Status:** Plan (not yet executed). Decisions encoded; implementation pending.
**Scope:** All three logging subsystems in `abides-core`, `abides-markets`,
`abides-gym` — primarily System B (per-agent `Agent.logEvent`) and System C
(`Kernel.summary_log`). System A (stdlib `logging`) is touched only at the
edges.
**Companion analysis:** [`docs/reference/logging-architecture.md`](../reference/logging-architecture.md).
**Audience:** Maintainers and contributors implementing the refactor.

---

## 0. Context and framing

ABIDES is a discrete-event market simulation. The per-agent event log
(`Agent.logEvent` → `agent.log` → `BZ2PickleLogWriter`) is functionally a
**drop-copy stream**: agents emit business events, an analytics layer
persists or aggregates them out-of-band of trading logic, downstream
tooling reconstructs state from the stream. Treating it as a drop-copy
clarifies the design: producers should not know about the analytics
layer, the analytics layer should be pluggable, and the on-disk format is
one implementation among many.

Three real problems today:

1. **Memory is unbounded.** `agent.log` grows linearly for the whole
   run; nothing flushes mid-simulation. The same applies to
   `OrderBook.book_log2` and `OrderBook.history`, which the exchange
   accumulates in-process and the runner reads at terminate. Hard
   ceiling for long financial simulations, parameter sweeps, Monte
   Carlo, and `abides-gym` training loops alike.
2. **End-of-run cost is high.** `to_pickle(compression="bz2")` is the
   slowest pickle path in pandas. `parse_logs_df`
   ([abides-core/abides_core/utils.py L154-L186](../../abides-core/abides_core/utils.py#L154-L186))
   is *not* O(N²) per row — it builds one `DataFrame` per agent and
   concats over `num_agents` frames — but it is still expensive: the
   per-row `dict()` reshape and column-widening at
   `pd.DataFrame(messages)` dominate, and the result has no streaming
   path.
3. **Three subsystems are conflated.** Operator stdout (System A), the
   load-bearing per-agent record (System B), and the vestigial
   `summary_log` (System C) share one folder, one config flag
   (`skip_log`), and no documentation about who reads what.
4. **No first-class observability surface for analytics consumers.**
   Notebooks, research scripts, production replay tooling, and gym
   environments all reach into `agent.log` / `OrderBook.book_log2` /
   `OrderBook.history` directly. There is no pluggable channel for an
   analytics consumer to subscribe to a typed stream of events without
   either taking on the full pickle path or instrumenting agents by
   hand.

The kernel improvement work has already carved part of the seam:

- `LogWriter` Protocol (`abides_core/log_writer.py`) abstracts the
  *terminal write* (`NullLogWriter`, `BZ2PickleLogWriter`).
- `KernelObserver` Protocol (`abides_core/observers.py`) plus
  `Agent.report_metric()` carries numeric KPIs out-of-band of the event
  stream.

What is missing is the **producer-side** seam between agents/order book
and the analytics layer. This plan adds that seam in a way that is
**lighter than the current implementation on the hot path** — not just a
clean abstraction layered on top.

**Framing — financial simulation observability first.** The primary use
case for `abides-ng` is running financial simulations and studying their
results: market-microstructure research, strategy backtests, parameter
sweeps, Monte Carlo, replay-driven analysis, and production-style drop-
copy. Each of these wants the same thing — a typed, ordered, pluggable
stream of events that downstream tooling can subscribe to without
rebuilding the simulator. The drop-copy framing is not a metaphor: a
sink consuming `ORDER_*` and `EXEC` events from this bus is functionally
the same shape as a FIX drop-copy session, which makes the design
portable to external analytics, message brokers, or columnar stores
without further rework.

Secondary consumers include `abides-gym` and other tight inner loops
(parameter sweeps, Monte Carlo). For those workloads the §3.4 pre-bound
no-op publish matters because it executes one Python call into a
zero-body function with the **exact positional signature** of the real
`publish_*` method — no `*args` tuple allocation, no `**kwargs` dict
allocation, no attribute load on the producer, no truthiness test.
`markets_environment` already mutes `book_logging` and
`exchange_log_orders` by hand
([abides-gym/abides_gym/envs/markets_environment.py L54-L55](../../abides-gym/abides_gym/envs/markets_environment.py#L54-L55))
for exactly this reason; the new path generalizes that pattern to all
record kinds and removes the per-publish branch entirely.

---

## 1. Goals (in priority order)

1. **First-class analytics observability.** Provide a typed, ordered,
   pluggable event stream that downstream tooling — notebooks,
   research scripts, replay analytics, drop-copy consumers, parameter
   sweeps — can subscribe to without reaching into agent internals or
   taking on the legacy pickle path. The stream is the public contract;
   sinks are pluggable. This is goal #1 because it is what makes the
   simulator useful as a research tool beyond a single config.
2. **Fully decouple the kernel from the analytics layer.** The kernel
   knows it has a *channel*; it does not know what consumes it. Sinks
   are pluggable without touching agent or order-book code.
3. **Lower per-event hot-path cost** versus the current
   `if owner.book_logging: book_log2.append({...})` and
   `agent.log.append((time, type, payload))` paths. The new path must
   be measurably cheaper when the analytics layer is enabled, and
   essentially free (one Python call to a typed no-body function) when
   it is not.
4. **Bound memory** during long simulations. In-memory sinks use
   columnar storage; async-I/O sinks spill to disk at a high
   watermark instead of growing without bound.
5. **Tight-inner-loop throughput.** With no sinks configured, the
   producer hot path is one Python call into a typed zero-body
   function — no positional/keyword collection, no allocation, no
   attribute load. Benefits all tight-loop consumers: `abides-gym`
   training episodes, parameter sweeps, Monte Carlo. Quantified by
   Phase 0 benchmarks.
6. **Cut serialization cost** at simulation end. Replace the
   per-row `dict()` reshape and column-widening in `parse_logs_df`
   with a vectorized projection from the new columnar in-memory
   sink, and offer a streaming columnar path (`ParquetSink`) for
   workloads that exceed in-memory budgets.
7. **Logical equivalence with legacy artifacts during the
   deprecation window.** Until Phase 5 lands, a default-config run
   produces a `<run_id>/<AgentName>.bz2` artifact that
   *deserializes to equal Python objects*, with rows sortable to
   the same canonical order, as the current release. Byte-identical
   bz2 is **explicitly not guaranteed** — see §2 for rationale and
   the migration note.
8. **Stop conflating three subsystems** in code, configuration, and
   on-disk layout.

---

## 2. Non-negotiable invariants

Any implementation must satisfy all of the following. CI checks must
enforce them where possible.

- **Determinism.** Sinks observe events in publication order. Single-
  threaded dispatch (see §3.1) makes this true by construction.
  Optional async I/O sinks (e.g. Parquet) must produce output that
  **deserializes to equal Python objects** as the inline path for any
  fixed seed; pinned by a deserialized-equality regression test on a
  small reference run. Byte-identical bytes are not guaranteed (see
  logical-equivalence invariant below).
- **Total order is `(sim_time_ns, agent_id, seq)`** where `seq` is a
  per-bus monotonic 64-bit integer incremented on every successful
  publish, present in every wire tuple (§3.2). Consumers reconstruct
  canonical order from these three fields and nothing else.
- **No silent event drops at the bus boundary.** Every successful
  `publish_*` call is delivered exactly once to every sink whose
  declarative filter (§3.3) accepts it, until that sink raises (see
  sink-failure invariant below). Backpressure on async-I/O sinks (only
  possible there; see §3.5) **spills to disk** rather than blocking
  or dropping. The producer is never blocked by an analytics buffer.
- **Sink failure is not a silent drop.** When a sink raises in
  `on_event` / `on_metric` / `on_book_snapshot`, the bus marks the
  sink failed, **stops dispatching to that one sink only**, records
  the failure with sink id and exception, and surfaces it at
  `bus.shutdown()` per the configured policy (§3.7). Other sinks
  continue to receive every event. The bus also requires sinks to
  implement `on_event` etc. as transactional with respect to their
  own internal state — a sink that mutated half its column arrays
  before raising must restore them; the shipped `InMemorySink`
  satisfies this by computing column appends into a local list and
  committing only after all schema-driven splits succeed (§3.5).
- **Sinks must not publish from inside `on_*` hooks.** A sink that
  calls back into `bus.publish_*` during dispatch corrupts the
  publication-order invariant and the per-bus `seq` counter. Debug
  mode (`ABIDES_BUS_VALIDATE=1`) sets a re-entrancy flag during
  dispatch and asserts on violation.
- **Sinks must not mutate received tuples or their elements.** All
  payload tuples and the lists carried inside `book_snapshot` payloads
  (`bids`, `asks`) are treated as read-only by every sink. The bus
  does not defensively copy. Out-of-tree sinks that need a private
  copy must allocate one themselves.
- **Hot-path producer cost when no sink wants a record kind is one
  Python call into a typed zero-body function.** The bus binds, at
  `start()`, a per-kind module-level function with the exact
  positional signature of the **producer call site** (the bus injects
  `seq` internally; producers do not pass it). For `publish_event`:
  `def _noop_publish_event(agent_id, agent_type, sim_time_ns, event_type, payload): return None`
  — 5 positional args, matching every call site. The function is
  assigned to `self.publish_event` (instance attribute, not the class
  attribute, to bypass the descriptor protocol so `self` is not
  bound). No `*args`/`**kwargs` collection, no allocation, no
  attribute load on the publisher.
- **No new mandatory third-party dependencies.** `pyarrow` (for
  `ParquetSink`) stays optional and is import-guarded.
- **Logical equivalence with legacy artifacts during the deprecation
  window.** Until Phase 5 lands, a default-config run produces a
  `<run_id>/<AgentName>.bz2` artifact whose deserialized DataFrame is
  equal to the current release's (same row set, sortable to the same
  canonical order on `(sim_time_ns, agent_id, seq)`, equal column
  dtypes after normalization), pinned by an end-to-end test. Byte-
  identical bz2 is **not** guaranteed: bzip2 output is sensitive to
  Python version, pandas version, and pickle protocol. The migration
  note in `CHANGELOG.md` and `docs/reference/logging-architecture.md`
  flags this as a known break for any out-of-tree consumer that
  compares bz2 bytes directly.
- **Reproducibility contracts in `docs/project/reproducibility.md`
  remain intact** at the *simulation outcome* level (PRNG sequence,
  agent decisions, fill prices, order matching). Log-artifact
  reproducibility is the weaker logical-equivalence guarantee above.
- **Payloads carry no live references.** No event payload may retain a
  reference to a live `Agent`, `Message`, `OrderBook`, or mutable
  collection owned by a producer. Permitted payload shapes are:
  scalars (`int`/`float`/`str`/`bool`/enum value), tuples of scalars,
  and lists/tuples of tuples-of-scalars. Today
  [abides-markets/abides_markets/agents/exchange_agent.py L420](../../abides-markets/abides_markets/agents/exchange_agent.py#L420)
  (the catch-all `else` branch:
  `self.logEvent(message.type(), message)`) and
  [exchange_agent.py L993](../../abides-markets/abides_markets/agents/exchange_agent.py#L993)
  publish raw `Message` objects — these pin sender, recipient, the
  order they wrap, and (for some message types) book snapshots, for
  the lifetime of the agent log. The `to_dict()` callsites at lines
  406 and 414 already publish dicts and are not part of this leak.
  The two raw-`Message` sites are the most likely root cause of
  long-sim OOM. A debug-mode validator in the bus enforces this
  invariant: it walks each payload **recursively** to a small fixed
  depth (default 4) and rejects any node that is an instance of
  `Agent`, `Message`, `OrderBook`, `Order`, or any class registered
  in the per-process forbid-list. Release builds skip the check.
  There is **no carve-out**: book snapshots carry primitive
  `(price, qty)` tuples (§3.5), not back-references.
- **No formatted-string payloads.** Payloads must be structured data,
  not human-readable strings. Today `BEST_BID` / `BEST_ASK` /
  `LAST_TRADE` / `STOP_ORDER_ACCEPTED` allocate f-strings or `str(x)`
  on the hot path
  ([order_book.py L213](../../abides-markets/abides_markets/order_book.py#L213),
  [L219](../../abides-markets/abides_markets/order_book.py#L219),
  [L234](../../abides-markets/abides_markets/order_book.py#L234);
  [exchange_agent.py L722](../../abides-markets/abides_markets/agents/exchange_agent.py#L722)).
  Replaced by tuple schemas (`QUOTE`, etc.; see §3.9). The
  schema-validator unit test (§3.9) AST-walks every callsite of
  `bus.publish_event`, `bus.publish_metric`, `bus.publish_book_snapshot`,
  and the legacy `Agent.logEvent`, and rejects `f"..."` and `str(x)`
  payload literals. Note that `MARK_TO_MARKET` and `FINAL_HOLDINGS`
  publish *dicts*, not f-strings; they are part of the deepcopy-of-
  dict reduction in §3.9, not this group.

---

## 3. Architecture

### 3.1 Single-threaded, per-handler dispatch

A single internal `EventBus` owned by the kernel. One per kernel
instance; not a process global. **The bus does not own a thread, queue,
or lock.**

**Definition of a "handler invocation":** one call to
`Agent.receive_message(...)` or `Agent.wakeup(...)` that runs to
completion before the kernel pops the next event from its queue. This
is the only event-processing unit ABIDES exposes.

How it dispatches:

1. `bus.publish_<kind>(...)` appends a record to a tiny per-kind ring
   buffer (a plain Python `list` reused across handler invocations).
   No locking, no GIL handoff, no allocation beyond the record itself.
2. The kernel calls `bus.drain()` immediately after each handler
   invocation returns, before popping the next event from its queue.
   `drain()` walks the ring buffers and calls the appropriate hook on
   each registered sink, in publication order. Empty-buffer drains are
   one length check per kind.
3. Sinks that need to do real I/O (Parquet flush, network) **own their
   own writer thread internally**, consuming from their own private
   buffer at flush time. The bus knows nothing about that thread; it
   just hands the record over with a synchronous method call. The
   sink's thread is the only place a queue and lock exist.

**Drain cadence is fixed at per-handler.** Earlier drafts considered a
user-tunable integer batch; that option is dropped. Per-handler drain
is the only mode the bus supports because:

- It keeps the publication-order invariant trivially true (every
  publish in a handler reaches every sink before any agent sees side
  effects of that handler).
- It bounds the per-kind ring buffer to "records emitted by one
  handler" — typically O(1–10), never growing.
- It removes a configuration knob whose only documented use case
  (Parquet row-group batching) is already served better by the sink
  owning its own internal buffer.

Why a bus-owned drainer thread was rejected:

- ABIDES is CPU-bound under CPython. A drainer thread would only pay
  off when *the consumer releases the GIL* (`pyarrow` writes do;
  `pickle.dump` does not). Pushing the thread into the sink that
  actually benefits keeps the cost where the benefit is and keeps the
  bus zero-overhead.
- Per-handler drain gives sinks a natural batch boundary (Parquet row
  groups, DB transactions) without any explicit batching API.

The bus exposes lifecycle hooks `start(meta)`, `flush()`, `shutdown(meta)`
that fan out to all sinks. `shutdown()` calls `drain()` one last time,
then `on_simulation_end()` on each sink.

### 3.2 Tuple wire format, typed views at the edges

Records on the wire are **plain tuples**, not dataclasses. Producers
publish positional payloads; sinks decide whether to reify them.

The bus exposes **three** publish kinds. Agent-emitted events and
order-book-emitted events share `publish_event`; the `event_type`
string discriminates them. Metrics and book snapshots are separate
because their consumers and shapes are structurally distinct.

**Producer call signatures** (what every publisher writes):

```text
bus.publish_event(agent_id, agent_type, sim_time_ns, event_type, payload)
bus.publish_metric(agent_id, agent_type, sim_time_ns, key, value)
bus.publish_book_snapshot(symbol, sim_time_ns, bids, asks, depth)
```

**Wire-tuple shapes** (what sinks observe — bus injects `seq`):

```text
(agent_id, agent_type, sim_time_ns, event_type, payload, seq)
(agent_id, agent_type, sim_time_ns, key, value, seq)
(symbol, sim_time_ns, bids, asks, depth, seq)
```

- `bids` / `asks` are tuples of `(price_cents, qty)` int tuples,
  materialized eagerly by the publisher (see §3.5 — there is no
  deferred snapshot token).
- `seq` is a per-bus monotonic 64-bit `int` assigned by the bus inside
  the publish call, after the ring-buffer append. Producers do **not**
  pass it; the bus injects it. From the producer's call site,
  `publish_event(...)` takes five positional args; the on-the-wire
  tuple has six.

Internally, each ring-buffer entry is the argument tuple itself
(`tuple` of the args plus the assigned `seq`). No `__init__` on the
hot path.

Typed dataclass views (`EventRecord`, `MetricRecord`,
`BookSnapshotRecord`) live in `abides_core/event_records.py` as a
**public read-side API**: sinks that want them call
`EventRecord.from_tuple(t)` (a `classmethod` that unpacks positionally
— ~one attribute assignment per field, still faster than a dict).
`InMemorySink` and `ParquetSink` skip reification entirely and read
positional indices directly.

This eliminates per-event `__init__` cost and matches how the
`OrderBook` itself already deals with `(price, qty)` tuples.

**Wire-tuple field order is public per kind.** The positional layout
for each of the three kinds is part of the public contract and may
only change with a major version bump (see §3.10). The field order
is pinned by `WIRE_FIELDS_EVENT`, `WIRE_FIELDS_METRIC`, and
`WIRE_FIELDS_BOOK_SNAPSHOT` constants in `abides_core.event_records`,
and `EventRecord.from_tuple` is implemented in terms of those
constants.

**Ordering.** Total order is `(sim_time_ns, agent_id, seq)`. The bus
guarantees `seq` is strictly monotonic across all publishes on that
bus, regardless of kind. Consumers reconstruct canonical order from
these three fields and nothing else.

### 3.3 Two-tier filtering: declarative bus-side, callable sink-side

The bus performs **only** declarative filtering. Arbitrary user logic
never runs on the producer hot path.

```text
class EventSink(Protocol):
    # Declarative — read by the bus at start() to plan dispatch.
    accept_event_types:  frozenset[str] | None = None    # None = all; empty/missing = none
    accept_metric_keys:  frozenset[str] | None = None
    accept_book_symbols: frozenset[str] | None = None

    # Optional sink-side predicate, applied after the bus has decided to deliver.
    def predicate_event(self, t: tuple) -> bool: ...           # default: True
    def predicate_metric(self, t: tuple) -> bool: ...
    def predicate_book_snapshot(self, t: tuple) -> bool: ...

    def on_simulation_start(self, meta) -> None: ...
    def on_event(self, t: tuple) -> None: ...
    def on_metric(self, t: tuple) -> None: ...
    def on_book_snapshot(self, t: tuple) -> None: ...
    def flush(self) -> None: ...
    def on_simulation_end(self, meta) -> None: ...
```

If a sink wants a kind without filtering, it sets the corresponding
`accept_*` to `None` (the default). If it wants nothing of that kind,
it leaves the attribute unset (the Protocol default is "none"). If it
wants a fixed allowlist, it provides a `frozenset[str]`. Anything more
expressive lives in the sink, evaluated *after* the bus has already
decided to deliver — never on the publisher.

Order-book-emitted events (`LIMIT`, `EXEC`, `CANCEL`, `MODIFY`,
`REPLACE`, etc.) flow through `publish_event` with their `event_type`
set accordingly; sinks that care only about fills filter via
`accept_event_types=frozenset({"EXEC"})`. There is no separate
order-book event channel.

### 3.4 Pre-bound no-op publish

At `bus.start()`, for each record kind, the bus computes the union of
acceptors. If no sink accepts a kind, the bus **rebinds the
corresponding `publish_*` method on the instance to a typed zero-body
function with the exact positional signature**:

```python
# Module-level functions, defined once.
def _noop_publish_event(agent_id, agent_type, sim_time_ns,
                        event_type, payload):
    return None

def _noop_publish_metric(agent_id, agent_type, sim_time_ns,
                         key, value):
    return None

def _noop_publish_book_snapshot(symbol, sim_time_ns, bids, asks, depth):
    return None

# At bus.start():
if not any_sink_accepts_events:
    self.publish_event = _noop_publish_event
```

Producers see one Python call into a function whose body is a single
`return None`. **No `*args`/`**kwargs` collection, no allocation, no
attribute load on the producer.** This is materially cheaper than
today's `if self.owner.book_logging:` guard, which performs an
attribute load on `self.owner` plus a truthiness test on every order
book mutation.

When at least one sink accepts a kind, `publish_<kind>` is bound to
the real method, which:

1. Increment the per-bus `seq` counter.
2. Append the argument tuple (with `seq` injected) to the per-kind
   ring buffer.

No filter evaluation, no allocation other than the tuple itself, no
method dispatch chain.

**No mid-simulation sink registration.** Sinks must be registered
before `bus.start()`. After start, the rebind is fixed for the run.
Attempting to register a sink after start raises
`RuntimeError("sink registration after bus.start() is not supported")`.
This is documented as a hard constraint; notebooks that want to
attach an interactive capture must construct the bus with the capture
sink already registered.

### 3.5 Sinks shipped in this repository

All sinks are constructed declaratively from `SimulationConfig` (see
§6). Every sink supports zero-copy access to its accumulated data
through a documented attribute, so the runner / notebooks never walk
the bus.

- **`InMemorySink`** — Captures events in **parallel column arrays**
  (`array.array("q", ...)` for ints, plain `list` for objects). No
  list-of-dicts. Final conversion is a single
  `pd.DataFrame({col: arr, ...})`, which is materially faster than
  `from_records` and uses ~3× less peak memory than today's
  list-of-tuples-of-dicts. Replaces `agent.log` as the source of
  truth for `parse_logs_df`.

  **Column allocation is eager from the schema registry.** At
  `start()`, the sink walks `EVENT_TYPE_SCHEMA` (§3.9) and pre-
  allocates one column-set per known `event_type`. The per-publish
  path is then a single dict lookup (`self._cols[event_type]`) plus
  per-column `array.array.append`. Unknown event types fall through
  to a generic `(time, type, payload_obj)` triple-column store and
  emit a one-time warning at `flush()`; that path is the migration
  fallback only and is never hit in default runs once §3.11 closes
  the registry.

  **Transactional `on_event`.** Each `on_event` call computes the per-
  field column appends into local variables and commits them with one
  contiguous block of `array.append` calls; if any append raises (e.g.
  type mismatch), the sink restores the lengths of any columns it has
  already touched in this call before re-raising, so column lengths
  stay aligned. The bus's per-sink failure handler then marks the
  sink failed (§2 sink-failure invariant).

  **Bounded-lifetime use.** `InMemorySink` is for runs whose full
  log fits in RAM. There is no spill-to-disk. The Phase 0 long-sim
  benchmark (`peak_rss_long_sim`) gates on `ParquetSink`, not on this
  sink. Documentation explicitly directs long-horizon users to
  `ParquetSink`.

- **`BZ2PickleSink`** — Wraps the existing `BZ2PickleLogWriter`.
  Drains the in-memory column store at `on_simulation_end()` and
  writes the legacy `<run_id>/<AgentName>.bz2` artifact.
  **Best-effort, terminal-only.** The sink does not checkpoint mid-
  run; a SIGKILL or OOM-kill before `on_simulation_end()` loses every
  event captured since the last `start()`. This is documented as a
  known limitation; users who need crash safety must use
  `ParquetSink`. **Marked deprecated from Phase 5; removed two minor
  releases later.**

- **`ParquetSink`** — Columnar; one parquet file per `event_type`,
  struct-of-arrays schema. Snappy or Zstd compression. Owns its own
  writer thread and bounded internal buffer; the bus delivers tuples
  synchronously, the sink hands them off to its writer. Uses
  `pyarrow`, import-guarded; absence yields a clear error at sink
  construction, not at first write.

  **Crash-safe checkpointing.** Every `checkpoint_every_rows`
  (default 100_000) the writer fsyncs the current row group and
  renames it from `<run>/.partial/<event_type>.<seq>.parquet` to
  `<run>/<event_type>.<seq>.parquet`. A SIGKILL between checkpoints
  loses at most one buffer's worth of events. `read_parquet_logs`
  unions the numbered files in `seq` order. The final flush at
  `on_simulation_end()` writes the last partial group.

- **`MetricsObserverSink`** — Adapter that exposes the existing
  `KernelObserver` Protocol on top of the bus. The shipped
  `DefaultMetricsObserver` continues to work unchanged, registered as
  a metrics-only sink. Removes the dual dispatch path inside `Agent`.

- **`OrderBookSnapshotMemorySink`** — In-memory capture of book
  snapshots, indexed by symbol, in parallel columns. Ships with
  three **sampling modes**, chosen at construction:
  - `every_update` — record on every L2 mutation. Behavioural parity
    with today's `book_log2`.
  - `on_top_of_book_change` *(default)* — record only when L1
    actually moves. Typical analytics workload; for a busy book this
    is 1–2 orders of magnitude fewer records than `every_update`.
  - `interval_ns(n)` — downsample to a fixed cadence. Useful for
    long-horizon training runs.
  The sampling decision is evaluated **inside the sink**, on the
  drain-side, after the bus has already delivered.

  **Snapshots are materialized eagerly at publish time.** The publisher
  (the `OrderBook`) walks `get_l2_bid_data` / `get_l2_ask_data` once
  per mutation and passes immutable `tuple` of `(price_cents, qty)`
  tuples to `bus.publish_book_snapshot`. There is no deferred token
  and no back-reference to the order book in the payload. This is
  required for correctness: a single `ExchangeAgent.receive_message`
  invocation can mutate the book multiple times (e.g. a market order
  walking three price levels), and a deferred token would let every
  one of those mutations read the same end-of-handler state at drain
  time. Eager materialization is the only sound design.

  The `on_top_of_book_change` filter is implemented as **publisher-side
  fast-path L1 caching, not as L2 deferral.** The `OrderBook` keeps a
  `(last_bid_top, last_ask_top)` pair and skips the L2 walk plus the
  `publish_book_snapshot` call when neither has changed. The walk and
  publish only run on actual L1 movement.

  **Multi-sink correctness: loosest-mode-wins.** If *any* registered
  snapshot sink requests `every_update`, the publisher-side L1 cache
  is **disabled** for the whole run — the publisher walks L2 and
  publishes on every mutation, and each sink applies its own sampling
  filter on the drain side. The bus computes the effective publisher
  mode at `start()` by taking the strictest publish requirement
  (`every_update` > `interval_ns` > `on_top_of_book_change`) across
  all snapshot sinks, and communicates it to the order book once.
  This preserves the §2 "no silent event drops" invariant: a sink
  asking for `every_update` always gets every mutation. The perf win
  of L1 caching only applies when *every* configured snapshot sink
  is happy with `on_top_of_book_change` (the default).

- **`OrderBookHistoryMemorySink`** — In-memory capture of order-book-
  emitted events (`LIMIT`, `EXEC`, `CANCEL`, `MODIFY`, `REPLACE`),
  indexed by symbol, in parallel columns. Filters on
  `accept_event_types`; defaults to `frozenset({"EXEC"})` (fills only)
  — the dominant use case in `runner._extract_trades` — and is
  widened explicitly when callers need cancels/modifies.

Out-of-tree sinks (JSONL, SQLite, DB drop-copy, message brokers,
custom book aggregators) are documented as a supported extension
point; the library does not ship them.

### 3.6 Resilience: spill-to-disk, not block

Only async-I/O sinks (`ParquetSink`, future DB sinks) own buffers that
can fill. The bus itself does not buffer. The contract for any sink
that owns a buffer:

- If the internal buffer reaches its high watermark, **spill the
  oldest unwritten chunk to a temp file** in the run's spill
  directory and continue accepting. Reload at `on_simulation_end()`
  before the final write.
- The producer is never blocked. Simulated time is never stalled by
  analytics.
- Sinks expose `spill_count` and `spill_bytes` counters for
  observability; the kernel logs a one-line summary at terminate.

This is strictly better than blocking-the-producer or fail-fast modes:
it is invisible to the simulator (no time distortion), it never loses
events, and disk pressure is a much more graceful degradation than
either silent stall or hard failure on a multi-hour run.

**Crash safety vs. graceful failure.** The temp-file-then-rename
pattern (§3.7) handles disk-full and permission-denied. It does not
handle SIGKILL, OOM-kill, or host crash. For those, only `ParquetSink`
offers any guarantee, via the `checkpoint_every_rows` mechanism in
§3.5: data prior to the last checkpoint survives. `BZ2PickleSink` and
`InMemorySink` lose all in-memory data on a hard kill. Long-horizon
users must use `ParquetSink`.

### 3.7 Error handling

- Sink exceptions during `on_event` / `on_metric` / `on_book_snapshot`
  are caught **per sink** by the bus's `drain()` loop. The bus marks
  the sink as failed, stops dispatching to it, records the sink id,
  the kind that failed, the `seq` at which it failed, and the
  exception, and surfaces all collected failures at `bus.shutdown()`.
  Other sinks continue to receive every event. This satisfies the §2
  sink-failure invariant: failure is recorded and surfaced, not
  silently dropped.
- The shipped `InMemorySink` is transactional inside `on_event`
  (§3.5): a partial column-append failure restores column lengths
  before the exception propagates to the bus. Out-of-tree sinks are
  required to honour the same contract; the test in §8 covers it.
- Default kernel policy on sink failure: **fail-fast at terminate**
  with a clear log line indicating which sink failed, at which `seq`,
  and how many records it had accepted before failing. Configurable
  to `"warn"` for research workflows where partial logs are better
  than no logs.
- All terminal disk writes use **temp-file-then-rename** to avoid
  partial files on disk-full / permission-denied. Today a crash at
  terminate after a multi-hour sim leaves no artifact at all; this
  closes that hole.
- Sinks with internal threads must surface their own thread errors
  through their `flush()` / `on_simulation_end()` return path; the
  bus does not poll them.

### 3.8 Compatibility shims (deprecation window only)

- `OrderBook.book_log2` and `OrderBook.history` become **terminal-only
  cached properties** that materialize from
  `OrderBookSnapshotMemorySink` / `OrderBookHistoryMemorySink` on
  first access, cache the result as a tuple of frozen dicts, and emit
  `DeprecationWarning`. Subsequent accesses return the cached tuple.
  Removed in Phase 5+2.
- `Agent.log` becomes a similarly terminal-only cached property
  pointing at `InMemorySink`'s reconstructed list-of-tuples view, also
  returned as a tuple. Same lifecycle.

**Cached compatibility properties are intended for post-`shutdown()`
reads only.** Accessing them before the bus has been shut down is
supported but the result is a snapshot at access time; later publishes
do not invalidate or refresh the cache. The deprecation warning text
calls this out. The properties return immutable tuples to prevent the
old-style `agent.log.append(...)` direct-mutation pattern from
corrupting the sink's reconstructed view; any in-tree call site that
still mutates `agent.log` is migrated to `bus.publish_event` in
Phase 2 (audit gated by the AST validator in §3.9).

**Row-order under per-handler drain.** Reconstructed rows are sorted
on `(sim_time_ns, agent_id, seq)` (§2), which matches today's append
order for any single agent because per-agent publication happens
inside one handler invocation and `seq` is monotonic across the bus.
Code that depends on positional index (`agent.log[i]`) without
sorting first continues to match. The deprecation warning text calls
this out.

### 3.9 Payload schema registry — zero per-event allocation

The wire format (§3.2) is positional tuples. That gives consumers a
shape but not a *contract*: today an `ORDER_SUBMITTED` payload is
"whatever `order.to_dict()` happens to return," which makes any
downstream sink (Parquet, drop-copy, third-party) brittle and turns
implicit dict keys into a load-bearing public API the first time
someone writes a SQL query on the result.

We fix this without paying any per-event allocation cost.

**Core idea: schema is metadata, registered once at import time;
records on the wire stay tuples.**

```text
abides_core/event_payloads.py

# Constructed once at import. Never allocated per event.
@dataclass(frozen=True, slots=True)
class PayloadSchema:
    name:       str                         # e.g. "ORDER_EVENT"
    version:    int                         # bump on field rename/remove/reorder
    fields:     tuple[str, ...]             # positional field names
    arrow_types: tuple[pa.DataType, ...]    # for ParquetSink; lazy-built

# Schemas (one instance per logical payload shape, shared across
# many event_types).
# Single wide nullable schema with int8 discriminator. LIMIT=1, MARKET=2, STOP=3.
# Subclass-specific fields are nullable; consumers dispatch on order_kind.
ORDER_EVENT = PayloadSchema(
    "ORDER_EVENT", version=1,
    fields=("order_id", "order_kind", "symbol", "side", "qty",
            "limit_cents", "stop_price_cents", "tif",
            "is_hidden", "is_price_to_comply"),
    arrow_types=(pa.int64(), pa.int8(), pa.string(), pa.int8(),
                 pa.int64(),
                 pa.int64(),  # nullable: LIMIT only
                 pa.int64(),  # nullable: STOP only
                 pa.int8(),   # nullable: LIMIT only (TIF)
                 pa.bool_(),  # nullable: LIMIT only
                 pa.bool_()), # nullable: LIMIT only
)
HOLDINGS    = PayloadSchema("HOLDINGS", 1,
                            ("symbol", "delta_qty", "qty_after", "cash_after_cents"),
                            (pa.string(), pa.int64(), pa.int64(), pa.int64()))
CASH        = PayloadSchema("CASH",     1, ("cents",),  (pa.int64(),))
DEPTH       = PayloadSchema("DEPTH",    1, ("levels",), (pa.list_(pa.list_(pa.int64())),))
QUOTE       = PayloadSchema("QUOTE",    1,
                            ("symbol", "price_cents", "qty"),
                            (pa.string(), pa.int64(), pa.int64()))
AGENT_TYPE_ = PayloadSchema("AGENT_TYPE", 1, ("name",), (pa.string(),))
EMPTY       = PayloadSchema("EMPTY",    1, (),         ())

# Single source of truth: event_type → schema.
EVENT_TYPE_SCHEMA: dict[str, PayloadSchema] = {
    "ORDER_SUBMITTED":    ORDER_EVENT,
    "ORDER_ACCEPTED":     ORDER_EVENT,
    "ORDER_EXECUTED":     ORDER_EVENT,
    "ORDER_CANCELLED":    ORDER_EVENT,
    "STOP_TRIGGERED":     ORDER_EVENT,
    "MODIFY_ORDER":       ORDER_EVENT,
    "REPLACE_ORDER":      ORDER_EVENT,
    "CANCEL_SUBMITTED":   ORDER_EVENT,
    "CANCEL_PARTIAL_ORDER": ORDER_EVENT,
    "STARTING_CASH":      CASH,
    "ENDING_CASH":        CASH,
    "MARK_TO_MARKET":     CASH,
    "FINAL_VALUATION":    CASH,
    "HOLDINGS_UPDATED":   HOLDINGS,
    "BEST_BID":           QUOTE,
    "BEST_ASK":           QUOTE,
    "LAST_TRADE":         QUOTE,
    "BID_DEPTH":          DEPTH,
    "ASK_DEPTH":          DEPTH,
    "AGENT_TYPE":         AGENT_TYPE_,
    "MKT_CLOSED":         EMPTY,
    ...
}
```

**Producer side: tuples replace dicts.**

Today's `ORDER_SUBMITTED` path allocates more than a single dict. The
call chain is:

```python
# abides-markets/abides_markets/orders.py:101-104
def to_dict(self) -> dict[str, Any]:
    as_dict = deepcopy(self).__dict__   # invokes LimitOrder.__deepcopy__
    as_dict["time_placed"] = fmt_ts(self.time_placed)
    return as_dict
```

`deepcopy(self)` invokes the subclass `__deepcopy__` (e.g.
[orders.py L162-L186](../../abides-markets/abides_markets/orders.py#L162-L186)
for `LimitOrder`), which constructs a fresh `LimitOrder` instance plus
a `deepcopy(self.tag)` when `tag` is non-trivial. So every `ORDER_*`
event today allocates, in the steady-state `tag=None` case:

1. A new `LimitOrder` instance (~280 B header + per-instance dict).
2. The `fmt_ts(...)` string for `time_placed`.
3. The outer `(time, type, payload_dict)` tuple in `Agent.logEvent`.

That is **three allocations per ORDER_* event** in the common case;
four when `tag` is a non-primitive that triggers `deepcopy(self.tag)`.
(`as_dict = deepcopy(self).__dict__` does not allocate a new dict —
it references the dict already created by the new `LimitOrder`'s
`__init__`.)

After this change, `Order` grows a single
`to_payload_tuple(self) -> tuple` method that reads its `__slots__`
into a positional tuple matching `ORDER_EVENT.fields`. The chain
collapses to **one tuple allocation** (the wire payload) plus the
ring-buffer append. That is roughly a **3× reduction in per-order-event
allocation count** in the common case, plus a per-instance memory win
from slotting `Order` (§4).

```python
# Before
self.logEvent("ORDER_SUBMITTED", order.to_dict(), deepcopy_event=False)

# After
bus.publish_event(self.id, "TradingAgent", t,
                  "ORDER_SUBMITTED", order.to_payload_tuple())
```

`HOLDINGS_UPDATED` today fires from five sites in
`abides-markets/abides_markets/agents/trading_agent.py` ([lines 283,
1139, 1232, 1259, 1289](../../abides-markets/abides_markets/agents/trading_agent.py#L283)),
each with `deepcopy_event=True` against the *entire* `self.holdings`
dict. That cost scales with portfolio breadth — fine for toy
single-symbol configs, expensive the moment a strategy spans many
symbols. The new schema is the **per-fill delta**, not the snapshot:
`(symbol, delta_qty, qty_after, cash_after_cents)`, one 4-int tuple
(~64 B) per fill.

This is a **behavioural change for downstream analytics that diff
holdings snapshots over time**. Any consumer that today reads a
sequence of `HOLDINGS_UPDATED` payloads and computes deltas externally
will instead consume the deltas directly. `InMemorySink` exposes a
`reconstruct_holdings(agent_id, at_time_ns) -> dict` convenience
method that materializes a snapshot by replaying deltas. This helper
must ship with Phase 2, not be deferred — it is the migration path
for existing notebook code, not an optional convenience. Eliminates
the per-fill deepcopy entirely.

`MKT_CLOSED` and other empty payloads use the **module-level singleton**
`EMPTY_PAYLOAD = ()` — zero allocation per event.

For scalar payloads (`STARTING_CASH`, etc.) the payload **is the
scalar**, not a 1-tuple. The schema's `fields=("cents",)` tells the
consumer "interpret the raw payload as the named scalar `cents`." This
keeps the today-shape (`logEvent("STARTING_CASH", 10_000)`) and avoids
even one tuple allocation per event.

**Enums are stored as `int8`, not as Python enum objects.** `Order.side`
is `Side` and `LimitOrder.time_in_force` is `TimeInForce`
([orders.py L14, L25](../../abides-markets/abides_markets/orders.py#L14)).
Parquet has no native enum type and pickled enums are not portable
across schema versions. **Both enums are converted to `IntEnum` with
stable integer values** (`Side.BID = 1`, `Side.ASK = 2`,
`TimeInForce.GTC = 1`, etc.). `to_payload_tuple()` reads the slot
directly; no encoding lookup on the publish path. The schema records
the enum class so the read-side `EventRecord.from_tuple` rehydrates
to the typed enum on demand.

**Migration note.** This is a **breaking change** for any out-of-tree
consumer that reads `order.side.value` and expects `"BID"`/`"ASK"`. A
deprecation-window helper `Side.legacy_str(self) -> str` returns the
old string form; the `CHANGELOG.md` entry for Phase 2 documents the
break and links to the helper. The publish hot path stays at zero
added cost (one slot read per enum field, no dict lookup, no method
call).

So the rule is: **payload arity matches schema arity**. Arity 0 → the
shared `()` singleton. Arity 1 → the bare scalar. Arity ≥ 2 → a
tuple. The schema records this and consumers honor it via a tiny
`unwrap(schema, payload)` helper that always returns a tuple-shaped
view (constructing a 1-tuple lazily *only* when a consumer asks; the
hot path never does).

**Consumer side: vectorized projection from columns. No per-record
reification.**

`InMemorySink` is already columnar (parallel arrays per kind). Add a
*per-event-type* sub-bucketing inside its event store: when an event
arrives whose `event_type` is in the schema registry and whose schema
has arity ≥ 2, the sink splits the payload tuple into per-field
columns *for that event_type*. End-of-run materialization is one
`pd.DataFrame({field: arr for field, arr in zip(schema.fields, cols)})`
call per event_type — same cost model as today's `parse_logs_df`,
done once, not per record.

`ParquetSink` reads `EVENT_TYPE_SCHEMA` at `start()`, derives one
Arrow schema per `event_type` from `(fields, arrow_types)`, and
opens one writer per type. Per event: one `append` to a typed array
buffer; flush on watermark. No `payload_json` column, no
per-event reification, no JSON encode.

Out-of-tree sinks read the registry the same way. The schema *is* the
public contract.

**Validation, opt-in only.**

A debug-mode bus (`ABIDES_BUS_VALIDATE=1`) wraps `publish_event`,
`publish_metric`, and `publish_book_snapshot` with:

- An arity check (`len(payload) == len(schema.fields)` for arity ≥ 2;
  type check for arity 1).
- A **recursive** `isinstance` check (default depth 4) enforcing the
  §2 invariant: no `Agent`, `Message`, `OrderBook`, or `Order`
  instance may appear at any level of the payload, including inside
  lists/tuples/dicts. Deeper nesting (rare in practice) is rejected
  with a clear error directing the publisher to flatten.
- A re-entrancy flag enforcing the §2 "sinks must not publish" rule.

Off by default — production runs pay zero. CI runs the test suite
with `ABIDES_BUS_VALIDATE=1`.

A unit test (`test_event_payload_schema.py`) `ast.parse`s every call
site of `bus.publish_event`, `bus.publish_metric`,
`bus.publish_book_snapshot`, and the legacy `Agent.logEvent`,
extracts the literal `event_type` (or `key` for metrics), asserts
it appears in `EVENT_TYPE_SCHEMA` (resp. the metric-key allowlist
if one exists), and rejects payload expressions that are f-strings
(`ast.JoinedStr`) or `str(x)` calls. The test also rejects any
in-tree `agent.log.append(...)` or `OrderBook.book_log2.append(...)`
statement after Phase 2 lands. New event types cannot land without
a schema entry; no callsite can silently regress to a stringified
payload or to direct list mutation.

**Schema evolution.**

Each `PayloadSchema` carries a `version: int`. Renaming or
reordering fields bumps it. `ParquetSink` writes
`{schema_name: version}` into file metadata; `read_parquet_logs`
checks it. The `EventBus` bumps a `bus_format_version` constant
covering the tuple wire format itself, kept separate from individual
payload schemas to allow independent evolution.

**Net cost vs. today.**

| | Today | After §3.9 |
|---|---|---|
| Per `ORDER_*` allocations (`tag=None` common case) | 3 (new `LimitOrder` + `fmt_ts` string + outer 3-tuple) | 1 tuple |
| Per `BEST_BID`/`BEST_ASK`/`LAST_TRADE` allocations | 1 f-string (parse spec, intermediates, concat) | 1 tuple of 3 ints |
| Per `HOLDINGS_UPDATED` allocations | 1 deepcopy of full `self.holdings` dict (scales w/ portfolio breadth) | 1 tuple of 4 ints |
| Per `MKT_CLOSED` allocations | 1 `None` ref | 1 `()` ref (shared) |
| Per `STARTING_CASH` allocations | 1 int ref | 1 int ref |
| Per-publish enum encoding cost | n/a | 0 (IntEnum slot read) |
| Schema lookups on hot path | 0 | 0 (debug mode only) |
| Reified payload objects per event | 0 (dict counts as raw) | 0 |
| Live references retained in payload | unbounded for the **two** raw-`Message` callsites at [exchange_agent.py L420](../../abides-markets/abides_markets/agents/exchange_agent.py#L420) (catch-all `else`) and [L993](../../abides-markets/abides_markets/agents/exchange_agent.py#L993) (stop-trigger handling); the `to_dict()` callsites at L406 and L414 are unaffected | none (§2 invariant, debug-checked recursively) |

The schema registry is **pure metadata**, allocated once at module
import, with no per-event cost. Consumers gain a versioned, typed,
discoverable contract. Producers shed dict construction in favor of
cheaper tuple construction.

### 3.10 Public vs. internal API boundary

After this refactor, the new modules split cleanly into a small
public surface and a larger internal one. This split is binding for
semver going forward.

**Public (semver-stable, breaking changes require a major bump):**

- `abides_core.event_payloads` — `PayloadSchema`, `EVENT_TYPE_SCHEMA`,
  the named schema instances (`ORDER_EVENT`, `HOLDINGS`, …),
  `unwrap(schema, payload)`. This is the consumer contract.
- `abides_core.event_records` — `EventRecord`, `MetricRecord`,
  `BookSnapshotRecord`, their `from_tuple(...)` classmethods, and the
  per-kind wire-field-order constants (`WIRE_FIELDS_EVENT`,
  `WIRE_FIELDS_METRIC`, `WIRE_FIELDS_BOOK_SNAPSHOT`). The wire-tuple
  positional layout is part of the public contract per kind.
- `abides_core.event_bus.EventBus.publish_event` /
  `publish_metric` / `publish_book_snapshot` — method signatures and
  semantics. Every agent and order-book site calls these directly
  after Phase 2; out-of-tree producers do the same. Adding kinds is
  additive; changing an existing signature is a major bump.
- `abides_core.event_sinks.EventSink` Protocol — the extension point
  for out-of-tree sinks.
- `read_parquet_logs(...)` once Phase 3 lands.
- `abides_core.event_bus.bus_format_version` (module-level int).
  Bumped on any change to the wire-tuple positional layout.
- `SimulationResult.logs` / `.l1_snapshots` / `.l2_snapshots` /
  `.trades` / `.liquidity` shapes (already public; reaffirmed here).

**Internal (may change at any time without notice):**

- `EventBus` internals other than the three public publish methods
  and `bus_format_version`: drain mechanics, ring-buffer sizing, the
  no-op rebind implementation, `seq` counter mechanics. Note:
  `bus_format_version` is **public** (consumers persisting Parquet
  read it back from file metadata).
- All concrete sink implementations *except* the `EventSink`
  Protocol surface. `InMemorySink` internals (column splitting,
  bucketing strategy) are free to change.
- `ParquetSink` checkpoint file naming and spill directory layout.

**Wire-format rule for in-tree sinks.** `InMemorySink` and
`ParquetSink` may read by positional index for performance; they
are exempt from the consumer-side rule that out-of-tree code must
go through the schema registry, **because they are versioned
together with the wire format**. A targeted lint test enumerates the
allow-listed in-tree files and rejects any other in-tree consumer
that indexes a wire tuple positionally without going through
`from_tuple` or the `WIRE_FIELDS_*` constants.

This boundary is documented in `docs/reference/logging-architecture.md`
when Phase 2 ships, and pinned by an `__all__` audit test plus the
lint above.

### 3.11 Event vocabulary — final list (gating Phase 2)

A grep over `abides-markets` shows `logEvent(` callsites spread across
at least these files: `agent.py`, `kernel.py`, `trading_agent.py`,
`exchange_agent.py`, `noise_agent.py`, `value_agent.py`,
`order_book.py`, `base_execution_agent.py`, `pov_execution_agent.py`,
`adaptive_market_maker_agent.py` (and the rest of
`agents/market_makers/`). Phase 0 ships the exact callsite count and
full file list.

Payload shapes for semantically related events are inconsistent — for
example `STOP_ORDER_ACCEPTED` publishes `str(order)`
([exchange_agent.py L722](../../abides-markets/abides_markets/agents/exchange_agent.py#L722))
while `STOP_ORDER_SUBMITTED` publishes `order.to_dict()`. The order
book also emits a `<tag>_POST_ONLY` event with a `{order_id: int}`
dict payload
([order_book.py L289](../../abides-markets/abides_markets/order_book.py#L289)).

The schema registry in §3.9 needs one row per surviving event type,
and Phase 2 cannot ship until that table is finalized. This refactor
is the right time to **delete dead event types and standardize the
survivors**; doing it later is a breaking change to the public schema
contract.

The deliverable is a table in `docs/reference/logging-architecture.md`
with one row per surviving `event_type`, each row carrying:

| Column | Notes |
|---|---|
| `event_type` | Stable string identifier; the registry key. |
| `producer` | File and class that publishes it. |
| `frequency tier` | `per-tick` / `per-quote` / `per-fill` / `per-order` / `per-run`. Drives the perf budget. |
| `schema` | One of the §3.9 `PayloadSchema` instances. |
| `consumers` | Which sinks/notebooks/runner extractors read it today. |
| `disposition` | `keep` / `rename → X` / `merge with Y` / `delete (no consumer)`. |

The table is built bottom-up by walking every `logEvent` call site
in the files listed above. Standardization rules:

- Every `ORDER_*` event uses `ORDER_EVENT` schema. No mix of
  `order.to_dict()` and `str(order)` survives.
- Every quote event (`BEST_BID`, `BEST_ASK`, `LAST_TRADE`) uses
  `QUOTE` schema. No f-string payloads survive.
- Every `*_CASH` / `MARK_TO_MARKET` / `FINAL_VALUATION` event uses
  `CASH` schema. The `MARK_TO_MARKET` callsite at
  [trading_agent.py L1547](../../abides-markets/abides_markets/agents/trading_agent.py#L1547)
  already publishes a structured dict (not an f-string); it is
  re-shaped to the `CASH` tuple plus an optional human-readable
  summary at `INFO` log level.
- Order-book-emitted events (`LIMIT`, `EXEC`, `CANCEL`, `MODIFY`,
  `REPLACE`) flow through `publish_event` with `event_type` set
  accordingly; their payload is `ORDER_EVENT`. There is no separate
  order-book event channel (§3.2).
- Event types with no observed consumer — neither `runner._extract_*`,
  nor a notebook in `notebooks/`, nor an out-of-tree caller surfaced
  during Phase 0 — are deleted, not migrated.

**Acceptance:** Phase 2 cannot merge until the table is reviewed
and every surviving `event_type` has a `PayloadSchema` row in
`EVENT_TYPE_SCHEMA`. The AST-walking validator test fails the build
on any callsite whose `event_type` is not in the registry.

---

## 4. Concrete changes by module

This is a high-level map, not a final code review.

- `abides_core/agent.py`
  - `logEvent` becomes one method call: `bus.publish_event(...)`. When
    no sink accepts events, `bus.publish_event` is the pre-bound
    no-op.
  - `report_metric` becomes `bus.publish_metric(...)` on the same
    bus.
  - `append_summary_log` parameter on `logEvent` is removed; the
    "important events" set becomes a sink-side allowlist (see §5).
  - `self.log` is removed from `Agent` itself; the deprecated
    cached property in `SimulationResult` covers existing readers.
  - **Direct `agent.log.append(...)` callers are migrated, not
    bridged.** Phase 0 ships a grep over `abides-core`,
    `abides-markets`, and `abides-gym` for any `\.log\.append\(`
    site whose receiver is an `Agent`. Each site is rewritten to
    `bus.publish_event(...)` with a registered schema; there is no
    fallback shim. The AST validator in §3.9 fails the build on any
    such residual call after Phase 2.
  - `Agent.log_events`
    ([abides-core/abides_core/agent.py L67](../../abides-core/abides_core/agent.py#L67))
    becomes a **deprecated no-op** in Phase 4. Subsumed by sink-side
    declarative filters (`accept_event_types`) and the §3.4 no-op
    rebind. Configs that set it get a `DeprecationWarning` pointing
    at the equivalent sink config. Removed in Phase 5+2. Without this
    deprecation the system has *four* overlapping mute switches after
    the refactor (`log_events`, `log_orders`, `book_logging`, sink
    filters) and every publish pays for both the new declarative gate
    and the legacy `if self.log_events:` check — strictly worse than
    today.
  - **Note on the `log_to_file` interaction.** Today
    [agent.py L73](../../abides-core/abides_core/agent.py#L73)
    sets `self.log_to_file = log_to_file & log_events`, so disabling
    `log_events` also suppresses file emission. After the refactor
    that interaction collapses naturally (no sinks accept → no file
    output), but the deprecation `DeprecationWarning` text must call
    out the equivalent sink-config replacement explicitly so users
    don't end up with both flags set and unintentionally enabled
    sinks.
- `abides_markets/abides_markets/orders.py`
  - Convert `Order`, `LimitOrder`, `MarketOrder`, `StopOrder` to
    `__slots__`. **Precondition** for cheap `to_payload_tuple()`
    (slot-tuple read instead of N attribute lookups by name) and a
    ~280 B/order RSS reduction (no per-instance `__dict__`). The
    existing custom `__deepcopy__` makes pickle compatibility
    painless.

    **Breaking-change risk.** Two real concerns the implementer must
    address:

    1. The `tag` field is documented at
       [orders.py L74](../../abides-markets/abides_markets/orders.py#L74)
       as a place callers attach arbitrary metadata. Tests, notebooks,
       and out-of-tree strategies may attach attributes other than
       `tag` directly to `Order` instances; slotting breaks that.
       Mitigation: in-tree audit lands in Phase 0; surviving
       attachers are migrated to use `tag`. Out-of-tree breakage is
       documented in `CHANGELOG.md` for Phase 2.
    2. `Order.__eq__` at
       [orders.py L106](../../abides-markets/abides_markets/orders.py#L106)
       compares `self.__dict__ == other.__dict__`. Slotted instances
       have no `__dict__`. The Phase 2 PR rewrites `__eq__` to
       compare the slot tuple; a regression test asserts equivalence
       on every existing `Order` subclass.
  - Add `to_payload_tuple(self) -> tuple` on each subclass returning
    a positional tuple matching `ORDER_EVENT.fields`. `Side` and
    `TimeInForce` are converted to `IntEnum` with stable integer
    values (§3.9); the slot already holds the int representation,
    so `to_payload_tuple()` reads it directly with no encoding cost.
  - Drop the `deepcopy(self).__dict__` antipattern in
    `to_dict()` (`orders.py:101-104`). Its only purpose was guarding
    against later mutation of the published dict; the new sink
    contract — sinks must not mutate received tuples — makes the
    guard unnecessary. `to_dict()` is retained only for the
    deprecation window as a thin wrapper around `to_payload_tuple()`
    for any out-of-tree code that depends on it; removed in Phase
    5+2.
- `abides_markets/abides_markets/agents/trading_agent.py`
  - `TradingAgent.log_orders` becomes a **deprecated no-op** in
    Phase 4 (same reasoning as `log_events`). It guards 25+ order-
    related callsites today; after Phase 2 those callsites publish
    unconditionally and the sink filters decide. Removed in Phase
    5+2.
- `abides_core/kernel.py`
  - Kernel owns an `EventBus`. Sinks are constructed from
    `SimulationConfig` (see §6) at `Kernel.__init__` or injected
    directly for tests.
  - **Bus injection.** `Kernel` sets `agent.bus = self.bus` at the
    same point in `Kernel.__init__` where it currently sets
    `agent.kernel = self`. Agents call `self.bus.publish_*(...)`
    directly. There is no `Agent.bus` getter that walks back through
    the kernel; the attribute is set once at construction and never
    rebound. A test in §8 asserts every constructed agent has
    `bus is kernel.bus`.
  - **`Agent.__init__` defaults `self.bus = _NULL_BUS`** (a
    module-level singleton in `abides_core.event_bus` whose
    `publish_*` methods are the same module-level zero-body functions
    used for the no-op rebind, §3.4). This guarantees bare-agent
    construction in tests never raises `AttributeError` on a publish
    call. The kernel overrides at construction. The null bus has no
    sinks and no `start()`/`drain()`/`shutdown()` lifecycle — it is
    purely a sentinel.
  - The kernel main loop calls `bus.drain()` **after every
    `wakeup`/`receive_message` return, before popping the next event
    from `event_queue`**. Pinned exactly: drain runs at the end of
    each handler invocation, not before the next one starts and not
    in the middle. `drain()` is a no-op in the common case (empty
    ring buffers if nothing was published since the last drain). The
    `Kernel.send_message` calls that a handler makes during execution
    do not themselves publish; they enqueue future messages whose
    own handlers will drain later.
  - `Kernel.append_summary_log` and `Kernel.write_summary_log` are
    removed in Phase 5; until then they delegate to a
    `LegacySummarySink` (see §5).
  - Terminate sequence: stop simulation → `bus.shutdown()` → existing
    `_log_writer` calls (deprecated branch) → observer
    `on_terminate()`.
- `abides_core/log_writer.py`
  - Marked deprecated in Phase 5. Kept until removal so out-of-tree
    code that wraps `BZ2PickleLogWriter` keeps working through the
    deprecation window.
- `abides_core/observers.py`
  - Unchanged externally. Internally re-implemented as a sink
    adapter.
- `abides_core/event_bus.py` (new)
  - `EventBus` with per-kind ring buffers, pre-bound no-op publish,
    `start` / `drain` / `flush` / `shutdown`. No thread, no queue,
    no lock.
- `abides_core/event_records.py` (new)
  - `EventRecord`, `MetricRecord`, `BookSnapshotRecord` dataclasses
    with `from_tuple` classmethods, plus the per-kind
    `WIRE_FIELDS_*` constants pinning positional layout (§3.10).
    Read-side API only. Order-book-emitted events flow through
    `EventRecord` like any other event — there is no separate
    `OrderBookEventRecord` (§3.2).
- `abides_core/event_sinks.py` (new) — `EventSink` Protocol,
  `InMemorySink`, `BZ2PickleSink`, `MetricsObserverSink`,
  `OrderBookSnapshotMemorySink`, `OrderBookHistoryMemorySink`.
- `abides_core/parquet_sink.py` (new, optional import) —
  `ParquetSink` with internal writer thread, spill-to-disk buffer,
  and the `checkpoint_every_rows` (default 100_000) crash-safety
  mechanism described in §3.5.
- `abides_markets/abides_markets/order_book.py`
  - Each existing `if self.owner.book_logging: self.append_book_log2()`
    site (six callsites in the order book at
    [L389-L391, L459-L461, L538-L539, L585-L587, L638-L639, L680-L682](../../abides-markets/abides_markets/order_book.py#L389))
    becomes a single
    `bus.publish_book_snapshot(symbol, sim_time_ns, bids, asks, depth)`,
    where `bids` and `asks` are immutable tuples-of-tuples produced
    by `get_l2_bid_data` / `get_l2_ask_data` (§3.5 eager
    materialization). No surrounding `if` — the bus's pre-bound no-op
    handles the "nobody wants this" case more cheaply than the
    current attribute-plus-truthiness check.
  - **Publisher-side L1 cache for `on_top_of_book_change` mode.** The
    `OrderBook` keeps a `(last_bid_top, last_ask_top)` pair updated
    on every mutation. When the configured snapshot sampling mode is
    `on_top_of_book_change` (the default), the publish call is
    skipped — and the L2 walk avoided — unless either top changed.
    `every_update` mode disables the cache and publishes on every
    mutation. `interval_ns` mode disables the cache and lets the sink
    handle filtering. The active mode is communicated to the order
    book once at config-build time so the per-mutation check is a
    single attribute compare.
  - Each `self.history.append(...)` site (LIMIT/EXEC/CANCEL/MODIFY/
    REPLACE) becomes a `bus.publish_event(symbol, sim_time_ns,
    event_type, payload)` with `event_type` set to `"LIMIT"` /
    `"EXEC"` / `"CANCEL"` / `"MODIFY"` / `"REPLACE"`. Same rationale:
    no surrounding guard needed.
  - `book_log2` and `history` become deprecated cached properties
    (§3.8). Removed in Phase 5+2.
- `abides_markets/abides_markets/agents/exchange_agent.py`
  - `book_logging` and `book_log_depth` constructor args become hints
    that translate to auto-registration of the two book memory sinks
    at config-build time (preserves backward-compatible defaults).
    Once `event_sinks` is set explicitly in config, the hints are
    ignored with a one-line info log. The `book_logging` flag itself
    becomes a **deprecated no-op** in Phase 4 (subsumed by the
    snapshot sink's `accept_book_symbols`); removed in Phase 5+2.
  - Replace the **two raw-`Message`-publish callsites** —
    [exchange_agent.py L420](../../abides-markets/abides_markets/agents/exchange_agent.py#L420)
    (the catch-all `else` branch:
    `self.logEvent(message.type(), message)`) and
    [L993](../../abides-markets/abides_markets/agents/exchange_agent.py#L993)
    (stop-trigger handling) — with payload tuples derived from each
    message's public fields. These are the actual leak sites; today
    they pin sender, recipient, the wrapped `Order`, and (for some
    message types) book snapshots for the lifetime of the per-agent
    log — the most likely root cause of long-sim OOM. The
    `to_dict()`-based callsites at
    [L406](../../abides-markets/abides_markets/agents/exchange_agent.py#L406)
    and [L414](../../abides-markets/abides_markets/agents/exchange_agent.py#L414)
    already publish dicts (not raw `Message` objects); they migrate
    to `to_payload_tuple()` for the per-event allocation win in
    §3.9, not as part of this leak fix. Each surviving event gets a
    dedicated schema entry in §3.9.
  - Replace the `STOP_ORDER_ACCEPTED` payload `str(order)`
    (`exchange_agent.py:722`) with `order.to_payload_tuple()`
    matching the `ORDER_EVENT` schema, harmonizing with
    `STOP_ORDER_SUBMITTED`.
- `abides_markets/abides_markets/simulation/runner.py`
  - `parse_logs_df` reads from `InMemorySink`'s parallel column
    arrays via a single `pd.DataFrame({col: arr, ...})` call
    (Phase 1 — independent of the rest). Note the current signature
    is `parse_logs_df(agents: list)`
    ([abides-core/abides_core/utils.py L154](../../abides-core/abides_core/utils.py#L154)).
  - `SimulationResult.logs` is sourced from `InMemorySink` instead
    of walking the per-agent `agent.log` lists.
  - `_extract_l1_close`, `_extract_l1_series`, `_extract_l2_series`
    read from `OrderBookSnapshotMemorySink` (still the same
    `book_log2`-shaped row dicts so `compute_l1_*` / `compute_l2_*`
    are unchanged).
  - `_extract_trades` and the VWAP path in `_extract_liquidity` read
    from `OrderBookHistoryMemorySink`.
- `abides_markets/abides_markets/config_system/models.py`
  - New fields described in §6.

---

## 5. Resolution of System C (`summary_log`)

**Decision: deprecate then remove.** (User-confirmed: "it is ok to
deprecate and later remove the old logging/pickle stuff.")

Rationale: every value System C provides is recoverable from System B
plus `report_metric`. The original consumer (a JPMorgan-internal
cross-run aggregator) was never open-sourced, no internal code reads
`summary_log.bz2`, and no documented external workflow depends on it.

Migration path:

1. **Phase 5 (deprecation release).** Introduce `LegacySummarySink`.
   `Kernel.append_summary_log` keeps working but emits a single
   `DeprecationWarning` per run pointing at this document. The
   `summary_log.bz2` artifact is still written so external tooling
   that may exist out of tree keeps functioning.
2. **Phase 5 + 1 (one minor release later).** Stop writing
   `summary_log.bz2` by default. Emit `DeprecationWarning` from
   `Kernel.append_summary_log` even when called.
3. **Phase 5 + 2.** Remove `Kernel.append_summary_log`,
   `Kernel.write_summary_log`, `Kernel.summary_log`,
   `LegacySummarySink`, the `append_summary_log=True` parameter on
   `Agent.logEvent`, and `LogWriter.write_summary_log` from the
   Protocol.

Users who need a cross-run roll-up of "starting cash, ending cash,
final valuation" register `MetricsObserverSink` plus a small custom
sink, or compute it from the per-agent logs. Both paths are documented
before Phase 5 ships.

---

## 6. Configuration surface

Promoted to `SimulationConfig.simulation`:

- `event_sinks: list[SinkConfig]` — declarative sink registry.
  Examples:
  - `[{kind: "memory"}]` — today's behaviour for agent events.
  - `[{kind: "memory"}, {kind: "orderbook_snapshot_memory"},
    {kind: "orderbook_history_memory"}, {kind: "bz2_pickle", root: "./log"}]`
    — the backward-compatible default during the deprecation window
    (book sinks auto-registered to keep `runner._extract_*` working).
  - `[{kind: "orderbook_snapshot_memory", symbols: ["ABM"],
    sampling: "on_top_of_book_change"}]` — the **"book-only fast
    access"** setup: nothing else is captured, no `agent.log` is
    built, no pickle is written, and `bus.publish_event` and
    `bus.publish_metric` are pre-bound no-ops. The post-sim caller
    reads `SimulationResult.l2_snapshots["ABM"]` and that's it.
  - `[{kind: "parquet", root: "./log", events: ["ORDER_*"],
    book_snapshots: {symbols: ["ABM"], sampling: "every_update"},
    compression: "zstd", checkpoint_every_rows: 100000}]` —
    production analytics setup. The selector keys (`events`,
    `metrics`, `book_snapshots`) map 1:1 to the Protocol fields in
    §3.3.
- `log_root: str` — currently buried in `Kernel(log_root=...)` only.
  Promoted so configs can drive it without touching the kernel
  constructor.
- `show_trace_messages: bool` — currently undiscoverable. Promoted to
  config.

**Multi-process runs.** Workers do not share a bus and do not write
into a shared `log_root`. The runner allocates a per-worker subdir
`<log_root>/worker_<rank>/<run_id>/...` and passes it to each worker's
`Kernel`. The naming pattern matches today's `parallel-simulation.md`
convention; the `read_parquet_logs` helper accepts a glob or a list
of such roots and concatenates. No cross-process queueing exists in
the bus.

**No mid-simulation sink registration.** Sinks are constructed before
`Kernel.run()` and never registered or removed mid-simulation. The
§2 invariant pinning the no-op rebind depends on this; the AST
validator and a runtime check in `EventBus.start()` enforce it.

Per-agent-type convenience (today requires loop-and-mutate at build
time):

- `disable_event_log_for: list[str]` — agent type names whose
  `log_events=False` is forced at build time. Replaces the only
  documented use of per-instance flags.

The existing `Agent.log_events` and `Agent.log_to_file` per-instance
flags remain for advanced cases, but the documented path becomes the
declarative one.

---

## 7. Phased rollout

Each phase is independently mergeable. No breaking change before Phase
5. Each phase ships with its own tests, micro-benchmark deltas, and
changelog entry.

### Phase 0 — Hot-path microbenchmark baseline

Before any architectural change, capture per-publish cost on a
representative `rmsc04`-class config for:

- `agent.log.append((time, type, payload))`
- `if owner.book_logging: book_log2.append({...})`
- `parse_logs_df` end-to-end on a 1M-row capture

There is no existing `benchmarks/` tree in the repo; Phase 0 ships
one, alongside a CI job that runs it on every PR touching a publisher
or sink. The four numbers below are **named acceptance gates** for
subsequent phases. No phase merges if its corresponding gate
regresses.

| Benchmark | Phase that gates on it | Target |
|---|---|---|
| `headless_sim_throughput_no_sinks` — a representative `rmsc04`-class simulation wall-clock with all sinks unconfigured | Phase 2 | **≥ 1.5×** today's release on the same machine. Headline financial-simulation throughput goal #1 from §1. |
| `gym_episode_throughput_no_sinks` — `markets_environment` episode wall-clock with all sinks unconfigured | Phase 2 | **No regression** vs today; ≥ 1.3× desirable. Tracks goal #5 (gym is a non-headline consumer of the same fast path). |
| `single_agent_run_with_default_sinks` — full default-config simulation | Phase 2, Phase 3a | **≤ 1.0×** today (no regression with the new path under the default sink set). |
| `peak_rss_long_sim` — long-horizon synthetic sim | Phase 3 | Bounded by `O(sink_buffer_high_watermark)`, **not** by event count. Peak in-memory event store is `Θ(M)` columnar arrays in one `InMemorySink`, not `Θ(N·M)` per-agent Python lists. |
| `parse_logs_df_p99` — `parse_logs_df` on a 1M-row capture | Phase 1 (legacy path) and Phase 3 (columnar path) | Phase 1 target: **≥ 1.3×** speedup against the current per-agent-`pd.DataFrame`-then-`pd.concat` path (single `from_records` build over the flat row list — the row format itself is unchanged in Phase 1). Phase 3 target: **≥ 5×** once `InMemorySink`'s columnar arrays land and `parse_logs_df` constructs the `DataFrame` directly from them. |

These numbers pin the design defaults. The Phase 2 / 3a
acceptance gates further down ("at least as fast as the old path")
are tied to these named benchmarks.

### Phase 1 — Quick wins (no architecture)

- **Vectorize `parse_logs_df` materialization.** Today
  ([abides-core/abides_core/utils.py L154-L186](../../abides-core/abides_core/utils.py#L154-L186))
  it builds one DataFrame per agent (each via
  `pd.DataFrame(messages)`), and concatenates them in a single
  `pd.concat` call. The Phase 1 win comes from collecting the flat
  row list across all agents and building **one** DataFrame via
  `pd.DataFrame.from_records`, eliminating the per-agent intermediate
  DataFrame allocation and column-widening. Target: **≥ 1.3×** wall
  clock on the `parse_logs_df_p99` benchmark. The larger ≥ 5× target
  moves to Phase 3 where `InMemorySink`'s parallel column arrays let
  `parse_logs_df` construct the `DataFrame` directly from the column
  store with no intermediate row reshape.
- Add `try/except` + temp-file-then-rename around all terminal
  `LogWriter` calls.
- Honour `skip_log` in `Kernel.write_summary_log`. Today the method
  ([abides-core/abides_core/kernel.py L853-L855](../../abides-core/abides_core/kernel.py#L853-L855))
  unconditionally calls `self.log_writer.write_summary_log(...)`,
  bypassing the `skip_log` flag honoured elsewhere; the fix is a
  one-line guard before the call.
- No public API change.

### Phase 2 — Introduce the bus and sink Protocol

- New modules: `event_bus.py`, `event_records.py`, `event_sinks.py`.
- Tuple wire format, pre-bound no-op publish, single-threaded
  per-handler dispatch (§3.1–3.4).
- Refactor `Agent.logEvent` and `Agent.report_metric` to publish
  directly on the bus.
- Default sink set: `InMemorySink` + `BZ2PickleSink` +
  `MetricsObserverSink` — chosen so externally observable behaviour
  is unchanged.
- `InMemorySink` uses parallel column arrays (§3.5).
- **Logical-equivalence reproducibility test** (§2): a small fixed-
  seed sim is run under both the legacy and new code paths; loading
  the `<run_id>/<AgentName>.bz2` artifact from each path produces
  the same set of `(time, type, payload)` rows after canonical sort
  on `(sim_time_ns, agent_id, seq)`. The legacy plan required
  *byte-identical* artifacts; that guarantee is dropped (see §2)
  because in-memory event ordering across multiple agents publishing
  in the same handler is sorted by `seq` rather than insertion order
  into a single shared list.
- **Acceptance gate:** Phase 0 microbenchmark shows the new path is
  at least as fast as the old path with the default sink set, and
  ≥ 10× faster when no sink is registered.

### Phase 3 — Ship `ParquetSink`

- Optional `pyarrow` import.
- `ParquetSink` owns its own writer thread and spill-to-disk buffer
  (§3.6). The bus does not gain a thread.
- Per-event-type Parquet schema doc; ship one schema per event type
  emitted by core agents.
- Decision needed before merge: per-event-type files vs single file
  with `payload_json` column (see §9).

### Phase 3a — Move order-book capture onto the bus

Independently mergeable from the rest of Phase 3 (only depends on
Phase 2's bus).

- Replace each `if owner.book_logging: append_book_log2()` and each
  `self.history.append(...)` site with the corresponding
  `bus.publish_*` call. **Drop the surrounding `if` guard** — the
  pre-bound no-op handles the disabled case more cheaply.
- Ship `OrderBookSnapshotMemorySink` and
  `OrderBookHistoryMemorySink`. Auto-register them when
  `ExchangeAgent.book_logging=True` and the user has not provided an
  explicit `event_sinks` list.
- Snapshot sink default sampling = `on_top_of_book_change`. A
  one-line config flag restores `every_update` for users who need
  byte parity with today's `book_log2`.
- Rewire `runner._extract_l1_close`, `_extract_l1_series`,
  `_extract_l2_series`, `_extract_trades`, and the VWAP path in
  `_extract_liquidity` to read from the two book sinks.
- Turn `OrderBook.book_log2` and `OrderBook.history` into
  deprecated **materialize-once cached** properties (§3.8); add a
  `DeprecationWarning`-on-first-access test.
- Reproducibility test: with `every_update` sampling configured,
  `SimulationResult.l1_snapshots`, `.l2_snapshots`, `.trades`, and
  `.liquidity` contain the same row set (after canonical sort on
    `(symbol, sim_time_ns, seq)`) as the previous release for a
    fixed seed. Byte-identical bytes are **not** required — see §2.
  with the default `on_top_of_book_change` is materially faster
  *and* uses materially less memory.

### Phase 5 — Deprecate `summary_log` and the legacy pickle path

- `BZ2PickleSink` and `Kernel.append_summary_log` emit
  `DeprecationWarning` once per run.
- Migration documentation links to this plan and to
  `MetricsObserverSink` / `ParquetSink` examples.
- Default `event_sinks` flips to `[{kind: "memory"},
  {kind: "parquet"}]` in a follow-up minor release.

### Phase 5 + 2 — Remove

- Delete `BZ2PickleLogWriter`, `BZ2PickleSink`,
  `Kernel.append_summary_log`, `Kernel.write_summary_log`,
  `Kernel.summary_log`, the `append_summary_log=True` parameter on
  `logEvent`, `LegacySummarySink`,
  `LogWriter.write_summary_log`, and the cached compatibility
  properties (`OrderBook.book_log2`, `OrderBook.history`,
  `Agent.log`) from the codebase.
- Update `parallel-simulation.md` on-disk layout section.
- Major version bump.

---

## 8. Test plan

A non-exhaustive list of tests required to land each phase. Existing
tests in `abides-markets/tests/test_pandas_integration.py`,
`test_simulation.py`, `test_replace_order_regression.py`,
`test_config_system.py`, and `abides-core/tests/test_kernel.py` are
the baseline; nothing in this plan is allowed to remove or weaken
their assertions.

- **Round-trip equivalence (Phase 2 onward).** A small fixed-seed sim
  is run under the legacy code path and the new bus path; the
  `(time, type, payload)` rows reconstructed from
  `<run_id>/<AgentName>.bz2` must match as **sets** after canonical
  sort on `(sim_time_ns, agent_id, seq)`. Byte equality of the bz2
  streams is **not** required (§2).
- **No-op publish (Phase 2).** With every sink unregistered,
  `bus.publish_event`, `bus.publish_metric`, and
  `bus.publish_book_snapshot` are each the literal pre-bound no-op
  function (asserted via `is`-identity check), and a 100k-publish
  loop completes within the budget set by Phase 0.
- **Backpressure correctness (Phase 3).** With a deliberately small
  Parquet sink buffer and a slow synthetic writer, no events may be
  lost; spill files must appear and be reloaded at terminate; the
  producer wall-clock is unaffected.
- **Crash-safe Parquet checkpoint (Phase 3).** A run is killed via
  `SIGKILL` between two `checkpoint_every_rows` boundaries; on the
  next process invocation, `read_parquet_logs` reconstructs every
  row written prior to the last checkpoint, partial-rename files in
  `.partial/` are skipped, and the row count matches `seq` minus
  at most one buffer's worth.
- **Filter short-circuit (Phase 3a).** When no sink accepts a kind,
  the call site must not allocate the payload (verified via a
  sentinel argument that raises `__repr__` or `__bool__` if invoked).
  Covers all three record kinds.
- **Book-only sink isolation (Phase 3a).** A run configured with
  *only* `OrderBookSnapshotMemorySink` produces correct
  `SimulationResult.l2_snapshots`, builds no `agent.log`, writes no
  pickle, and (asserted via `is`-identity check on
  `bus.publish_event`) does not even call into a real publish path.
- **Snapshot sampling parity (Phase 3a).** A small fixed-seed sim
  with `every_update` sampling produces a `.l2_snapshots` row set
  equal to the legacy path's after canonical sort on
  `(symbol, sim_time_ns, seq)`; `on_top_of_book_change` mode
  produces the L1-aligned subset (unit-checked against a hand-rolled
  filter applied to the `every_update` output).
- **Multi-mutation per handler correctness (Phase 3a).** A test
  drives a market order that walks three price levels in one
  `ExchangeAgent.receive_message` call. With `every_update`
  sampling, the snapshot sink records three distinct snapshots whose
  `bids`/`asks` payloads each match the book state at the moment of
  the corresponding mutation — not the end-of-handler state. This
  pins the eager-materialization decision in §3.5.
- **Cached compatibility property (Phase 3a).** Accessing
  `OrderBook.book_log2` returns an immutable tuple; subsequent
  publishes do not appear in the cached view; a `DeprecationWarning`
  is emitted on first access only.
- **Bus injection (Phase 2).** Every agent constructed by `Kernel`
  has `agent.bus is kernel.bus`. Asserted across a full default
  `rmsc04`-class config plus the `markets_environment` gym path.
- **Sink failure isolation (Phase 2 onward).** A sink that raises in
  `on_event` is marked failed and dropped from dispatch; other sinks
  continue to receive every subsequent event; the bus surfaces the
  failure (sink id, kind, `seq`, exception) at `shutdown()` per the
  configured policy.
- **Transactional column append (Phase 2).** A test injects a
  type-mismatched payload into `InMemorySink`; the resulting
  `on_event` exception leaves all per-event-type column arrays at
  their pre-call lengths (asserted via a length snapshot taken
  before the bad publish).
- **Reentrancy guard (Phase 2).** A sink whose `on_event` calls
  `bus.publish_event` triggers the bus's reentrancy detector and
  raises a clear error naming the offending sink. The bus is left in
  a usable state and continues to dispatch to other sinks for
  subsequent publishes.
- **Parquet schema stability (Phase 3).** Round-trip a recorded
  Parquet artifact through `pyarrow` and assert the schema matches
  the pinned reference for each shipped event type.
- **Determinism over multiple episodes (`abides-gym`).** Running N
  episodes back-to-back must produce the same final per-episode
  artifacts as N independent runs.
- **Memory bound (Phase 3).** A long-running synthetic sim with a
  Parquet sink keeps RSS under a configured ceiling; spill files
  appear and are cleaned up at terminate.

---

## 9. Open questions — to resolve before the relevant phase

These are intentionally unresolved in this plan. Each must be answered
in the implementation PR for the phase noted.

1. **Parquet layout (Phase 3).** Per-`event_type` parquet files (fast,
   small, typed; more files per run) vs one file with a `payload_json`
   string column (simpler, slower). Recommendation: per-type, but
   confirm with a benchmark on a representative sim.
2. **`pyarrow` weight (Phase 3).** `pyarrow` is ~50MB. Already a
   transitive dep of recent `pandas`, so likely free; confirm by
   inspecting the resolved environment in CI.
3. **Replay path (Phase 3).** Should `parse_logs_df` learn to read
   Parquet sink artifacts, or do we ship a separate
   `read_parquet_logs` helper? Recommendation: separate helper,
   `parse_logs_df` stays the canonical reader for the in-memory path
   only.
4. **Sink failure policy default (Phase 2).** Fail-fast (safe, may
   surprise research workflows) vs best-effort warn (lenient, may
   hide bugs). Recommendation: fail-fast, with a clearly documented
   config override.
5. **Multiprocess parallel runs.** Each worker owns its own bus and
   sinks; no cross-process queueing. §6 pins the
   `<log_root>/worker_<rank>/<run_id>/...` subdir convention.
   Confirm this matches the planned cross-run aggregation story
   before Phase 5.
6. **Deprecation timeline.** This plan says Phase 5 + 2 for removal.
   Confirm the absolute timeline in
   `docs/project/release-process.md` when Phase 5 is scheduled.
7. **Schema registry phasing (§3.9).** Land the registry and tuple
   payloads in Phase 2 alongside the bus (so `InMemorySink` can be
   columnar from day one), or land in Phase 3 just-in-time for
   `ParquetSink` (smaller blast radius)? Recommendation: Phase 2 —
   the producer-side dict→tuple swap is the disruptive part and
   should ride with the agent-API change, not be a follow-up.
8. **Schema additive-evolution policy.** Registering a new
   `event_type` should not be a major version bump. The policy
   should declare: adding an event type, adding an optional column
   to an existing `PayloadSchema`, or adding a sink kind are
   **additive** (minor bump); removing an event type, changing a
   column's dtype or position, or changing the meaning of an enum
   value are **breaking** (major bump). Document in
   `docs/reference/logging-architecture.md` alongside the registry
   table; ratify in Phase 2 PR.

---

## 10. What this plan does *not* change

- The stdlib `logging` subsystem (System A). It works. The only
  related improvement is making `show_trace_messages` discoverable
  via config (§6).
- The `KernelObserver` Protocol's external surface. It becomes a
  sink adapter internally; users registering observers see no change.
- The semantics of `Agent.report_metric` from the caller's point of
  view. It still pushes a numeric `(key, value)` to all interested
  observers.
- The `SimulationResult.logs` shape returned by `run_simulation`. It
  remains a `DataFrame` produced by `parse_logs_df` (now over the
  `InMemorySink` column arrays).
- The `SimulationResult.l1_snapshots` / `.l2_snapshots` / `.trades`
  / `.liquidity` shape. They remain identical; only the source of
  truth moves from `OrderBook.book_log2` / `OrderBook.history` to
  the two book sinks.
- Reproducibility guarantees. Single-threaded dispatch makes
  *logical* determinism true by construction; the row-set produced
  for a fixed seed is invariant. Byte-identical bz2 / Parquet
  artifacts are explicitly **not** guaranteed (see §2).

---

## 11. Performance budget and design constraints

This plan is performance-first. The constraints below are binding on
any implementation; if the implementation cannot meet them, the
design must be revisited rather than the constraint relaxed.

- **Hot-path publish, no-sink case:** one C-level call into a pre-
  bound module-level no-op function (typed signature, empty body).
  No attribute load on the publisher, no truthiness test, no dict
  lookup, no `*args`/`**kwargs` tuple/dict allocation. Strictly
  cheaper than today's `if self.owner.book_logging:` guard.
- **Hot-path publish, one-sink case:** one method call, one tuple
  construction, one `array.append`-class store. No filter
  evaluation, no dataclass `__init__`, no thread handoff. The bus
  injects a per-bus `seq` (one int increment).
- **Drain cost:** O(records since last drain) with a single
  attribute lookup per sink per kind. Empty-buffer drains are
  effectively free (one length check per kind).
- **Ordering:** total order is `(sim_time_ns, agent_id, seq)`,
  where `seq` is the per-bus monotonic counter injected by the bus
  on every successful `publish_*`.
- **Threads:** zero in core. Sinks that need them own them.
- **Memory:** in-memory sinks use parallel column arrays, not lists
  of dicts. Snapshot sink defaults to `on_top_of_book_change`
  sampling. Async-I/O sinks spill to disk at the high watermark
  rather than block or drop.
- **Peak in-memory event store:** `Θ(M)` columnar arrays in one
  shared `InMemorySink`, not `Θ(N·M)` per-agent Python lists like
  today's `agent.log`. For typical constant-`M`-per-agent workloads
  this is a `~agent_count×` memory reduction *before* any per-record
  encoding wins. This is a structural improvement that the
  per-event allocation work (§3.9) compounds, not duplicates.
- **Determinism:** logical equivalence (§2) is true by construction
  under single-threaded per-handler dispatch; pinned by row-set
  regression tests on every reproducibility-sensitive sink.
- **Payload schema:** metadata-only, registered once at import (§3.9).
  No per-event class allocation, no per-event registry lookup on the
  hot path. Producer payloads are bare scalars (arity 1), the shared
  `()` singleton (arity 0), or a tuple (arity ≥ 2) — strictly cheaper
  than today's per-event `dict`.

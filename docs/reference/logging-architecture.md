# ABIDES Logging — Architecture Reference

**Status:** Architecture reference.
**Scope:** The three logging subsystems present in `abides-core`,
`abides-markets` and `abides-gym`: the standard Python `logging`
module, the per-agent event stream that flows through `EventBus` to
registered `EventSink` implementations, and the deprecated
`summary_log` path. Includes the `OrderBook` capture flow that rides
on the same bus.
**Audience:** Developers writing or consuming logs from a simulation.

---

## 1. Overview — three subsystems, one bus

ABIDES has three logging subsystems that share the name "logging" but
do different things and barely interact.

| Subsystem | What it logs | Where it goes | Who reads it |
|---|---|---|---|
| **Python `logging`** | Lifecycle, periodic stats, debug traces | stdout (via `basicConfig`) | Operator watching the console |
| **`EventBus` event stream** ([`Agent.logEvent`][lev]) | Every business event an agent emits, plus metrics and order-book snapshots | `EventBus` → registered `EventSink` implementations | [`InMemorySink.agent_log()`][ims], [`parse_logs_df()`][pld], `MetricsObserverSink`, `ParquetSink`, notebooks |
| **`summary_log`** *(deprecated)* | A handful of "final state" events | `kernel.summary_log` list → `summary_log.bz2` | No in-tree consumer; pending removal |

The event-stream subsystem is the load-bearing one. Everything
downstream — metrics, plots, `SimulationResult.logs`, replay tooling —
reads from it.

[lev]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py
[ims]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/sinks/event_sinks.py
[pld]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/utils.py

> **See also:** [event-vocabulary.md](event-vocabulary.md) for the
> source-anchored inventory of every shipped `event_type` and its
> payload schema.

---

## 2. Python `logging`

### 2.1 Motivation

Operator output. Tells you the simulation is alive and roughly how
fast it is progressing. It is not a record of the simulation.

### 2.2 Concept

Every module follows the standard `logger = logging.getLogger(__name__)`
pattern. The kernel emits three classes of message:

- **Lifecycle (DEBUG)** — kernel init, agent attach/detach, queue
  begin/empty. Silent at INFO.
- **Periodic checkpoint (INFO)** — every 100 000 messages, one line:
  `--- Simulation time: ..., messages processed: ..., wallclock elapsed: ...s ---`.
  The only INFO output during the hot loop.
- **Trace (DEBUG)** — per-pop, per-dispatch, per-requeue lines inside
  `runner()` and `_enqueue()`. Gated by
  `logger.isEnabledFor(logging.DEBUG)` so the cost is one branch when
  DEBUG is off; very expensive when on.
- **Termination summary (INFO)** — event-queue elapsed, messages per
  second, per-agent-type mean ending value.

### 2.3 Configuration

`SimulationConfig.simulation.log_level` (default `"INFO"`) is plumbed
through to `logging.basicConfig(level=...)`. The format string is
fixed: `"[%(process)d] %(levelname)s %(name)s %(message)s"`.

There is no `FileHandler`, `RotatingFileHandler`, or any other handler
attached by ABIDES — output goes to stdout/stderr only. To capture
stdout to a file, attach a `FileHandler` yourself before
`run_simulation()`; the pattern is shown in
[parallel-simulation.md](parallel-simulation.md).

To enable trace lines, raise the kernel logger:

```python
import logging
logging.getLogger("abides_core.engine.kernel").setLevel(logging.DEBUG)
```

There is no kernel attribute toggle.

---

## 3. EventBus and sinks

### 3.1 Motivation

The simulation's record. Every agent business event, every metric,
and every order-book snapshot flows through a single
[`EventBus`][eb] instance owned by the `Kernel`. Storage policy
(memory, disk, Parquet, metrics observer) is decoupled from the
producer: callers register one or more [`EventSink`][es]
implementations and the bus fans events out to them.

[eb]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/telemetry/event_bus.py
[es]: https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/sinks/event_sinks.py

### 3.2 The three wire kinds

The bus carries three kinds of records, each as an index-ordered tuple
with a sink-side hook:

| Kind | Producer call | Sink hook | Used for |
|---|---|---|---|
| Event | `bus.publish_event(...)` (via `Agent.logEvent`) | `on_event` | Business events: orders, fills, holdings, etc. |
| Metric | `bus.publish_metric(...)` (via `Agent.report_metric`) | `on_metric` | Per-agent scalar metrics consumed by `KernelObserver`s. |
| Book snapshot | `bus.publish_book_snapshot(...)` (via `OrderBook`) | `on_book_snapshot` | L1 / L2 order-book snapshots. |

**Event wire tuple** (six fields, index-ordered — see
`WIRE_FIELDS_EVENT` in
[`event_records.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/telemetry/event_records.py)):

| Index | Field | Type |
|---|---|---|
| 0 | `agent_id` | `int` |
| 1 | `agent_type` | `str` |
| 2 | `sim_time_ns` | `int` (nanoseconds) |
| 3 | `event_type` | `str` |
| 4 | `payload` | `Any` (shape depends on schema; see §3.4) |
| 5 | `seq` | `int` (monotonically increasing) |

Metric and book-snapshot tuples have the same shape with different
field meanings (`WIRE_FIELDS_METRIC`, `WIRE_FIELDS_BOOK_SNAPSHOT`).
Typed views — `EventRecord.from_tuple(t)`, `MetricRecord.from_tuple(t)`,
`BookSnapshotRecord.from_tuple(t)` — are provided for readability.

### 3.3 `EventSink` Protocol

```python
@runtime_checkable
class EventSink(Protocol):
    accept_events: bool
    accept_metrics: bool
    accept_book_snapshots: bool

    def on_simulation_start(self, meta: dict) -> None: ...
    def on_event(self, t: tuple) -> None: ...           # 6-field wire tuple
    def on_metric(self, t: tuple) -> None: ...          # 6-field wire tuple
    def on_book_snapshot(self, t: tuple) -> None: ...   # 6-field wire tuple
    def flush(self) -> None: ...
    def on_simulation_end(self, meta: dict) -> None: ...
```

`EventBus.register(sink)` validates the Protocol at registration time
and raises `TypeError` with a missing-method list if the object does
not conform. The three `accept_*` class attributes are checked
explicitly and gate which `on_*` hooks the bus calls.

### 3.4 Payload schemas

Every `event_type` shipped by in-tree agents is registered in
[`EVENT_TYPE_SCHEMA`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/telemetry/event_payloads.py),
mapping the string key to a frozen `PayloadSchema(name, version, fields)`.
The `fields` tuple determines the on-wire payload shape:

| Arity (`len(fields)`) | On-wire payload | Example schema | Example call |
|---|---|---|---|
| 0 | `EMPTY_PAYLOAD` (the empty tuple `()`) | `EMPTY` | `self.logEvent("MKT_CLOSED", EMPTY_PAYLOAD)` |
| 1 | bare scalar (no tuple wrapping) | `CASH = (cents,)` | `self.logEvent("STARTING_CASH", 10_000_000)` |
| ≥ 2 | positional tuple of length `arity` | `ORDER_EVENT = (...)` | `self.logEvent("ORDER_ACCEPTED", order.to_payload_tuple())` |

`Order`, `LimitOrder` and `StopOrder` expose `to_payload_tuple()`
returning an `ORDER_EVENT`-shaped tuple. `Side` and `TimeInForce` are
`IntEnum`, so they cross the wire as integers;
`Side.legacy_str()` / `TimeInForce.legacy_str()` are available for
human-readable reporting.

Dynamic-name events that cannot appear in the static registry are
still bounded:

- `ExchangeAgent` echoes incoming `OrderMsg` subclasses under
  `message.type()` (e.g. `"LimitOrderMsg"`); every such class is
  registered in `EVENT_TYPE_SCHEMA` against `ORDER_EVENT`.
- Non-order ExchangeAgent receipts (query and subscription requests)
  log an `EMPTY` payload under the message class name; an allowlist
  bounds which classes are accepted, and anything else is
  warn-and-dropped via the stdlib logger.
- `OrderBook` post-only rejections emit dynamic
  `<order.tag>_POST_ONLY` events with a small dict payload; these
  fall through to the `GENERIC` bucket.

The schema is enforced at build time by
[`test_event_payload_schema.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/tests/test_event_payload_schema.py),
which walks every `.py` file in the three packages and inspects calls
to `logEvent`, `publish_event`, `publish_metric` and
`publish_book_snapshot`. The test fails if a string-literal
`event_type` is missing from `EVENT_TYPE_SCHEMA` or if the payload
argument is an f-string or a literal `str(x)` call.

### 3.5 Bus lifecycle

```
Kernel.__init__()        → EventBus() created; sinks registered.
Kernel.initialize()      → bus.start(meta)
                            • calls on_simulation_start on all sinks
                            • drains pre-start queue (e.g. AGENT_TYPE events)
                            • rebinds publish_* to real or no-op methods
Kernel.runner() per-tick → bus.drain()   (after each message dispatch)
Kernel.terminate()       → bus.shutdown(meta)
                            • drain() + on_simulation_end on all sinks
                            • rebinds publish_* to pre-start stubs
```

Events are **not** dispatched synchronously on `logEvent()`. They are
buffered and drained once per message dispatch in `runner()` and once
at `terminate()`. Code reading `InMemorySink` data outside the normal
lifecycle (e.g. tests that call only `initialize()`) must call
`kernel.event_bus.drain()` first.

### 3.6 Pre-init bootstrap

`Agent.__init__()` does not publish on the bus — it only allocates an
empty `_pre_init_log` buffer for subclasses that call `logEvent()`
from their own `__init__`. Each call to `Agent.kernel_initializing()`
publishes a fresh `AGENT_TYPE` event and flushes the pre-init buffer
to the bus it has just attached to. This guarantees `AGENT_TYPE` is
re-emitted on every kernel attach (the gym-reset pattern of building
a new `Kernel` with the same agent). All bootstrap events carry
`sim_time_ns=0`.

Because `bus.start()` has not been called yet, these events enter the
pre-start queue and are drained automatically when `bus.start()`
runs. The pre-start queue exists for all three wire kinds, so book
snapshots published before `start()` (e.g. by an oracle warm-up step)
are also delivered.

### 3.7 Failure isolation

A single `try/except` wraps the tuple loop for each sink in
`_drain_buffers()`. On exception:

- The sink is added to `_failed_sinks` and the failure tuple is
  appended to `_sink_failures` once; subsequent batches for the same
  sink are skipped entirely.
- The remaining tuples of the current batch are dropped for that sink
  only; other sinks see the full batch.
- The exception is logged at `ERROR` with `exc_info`.
- `bus.shutdown()` raises `RuntimeError` summarising all failed sinks;
  `Kernel.terminate()` catches and logs this rather than re-raising
  (conservative current behaviour).
- Failures are surfaced programmatically on
  `KernelRunResult.sink_failures` as a tuple of `SinkFailure` records
  (`sink_index`, `sink_type`, `exception_repr`). Callers that want to
  fail the run on any sink failure check this field after
  `kernel.run()`.

Per-batch (rather than per-tuple) wrapping avoids the overhead of
millions of `try/except` frames in the hot dispatch path and prevents
a known-broken sink from being re-invoked for every remaining tuple.

---

## 4. Shipped sinks

### 4.1 `InMemorySink`

Registered by default when `event_sinks=` is not passed to `Kernel`.
Stores events in a schema-driven columnar layout
(`_cols: dict[event_type, dict[column, list]]`) and metrics and book
snapshots as raw wire-tuple lists. Each event-type bucket carries the
four common wire columns (`agent_id`, `agent_type`, `sim_time_ns`,
`seq`) plus one column per `PayloadSchema` field; events whose
`event_type` is missing from `EVENT_TYPE_SCHEMA` fall through to a
single `payload` column under the `GENERIC` schema with a one-time
warning.

Payload shape is validated against the chosen schema before any column
is touched, so appends are transactional: a mismatched row is diverted
to a per-type `"<event_type>::generic"` fallback bucket rather than
leaving a typed bucket torn.

Key API:

- `agent_log(agent_id)` → `list[tuple[int, str, Any]]` of
  `(sim_time_ns, event_type, payload)` triples, sorted by `seq`.
- `events`, `metrics`, `book_snapshots` — wire-tuple lists. `events`
  is rebuilt on demand from the columns; cache the result if you scan
  it more than once.
- `columns` → the raw `dict[event_type, dict[column, list]]` mapping
  for zero-copy analytics paths (e.g. Arrow exporters).
- `bucket_schema(event_type)` → the `PayloadSchema` chosen for a
  given bucket, or `None`.
- `to_dataframe()` → wide-flat `pd.DataFrame` of all events with
  `WIRE_FIELDS_EVENT` columns.

The convenience accessor `kernel.event_bus.in_memory_sink` returns the
first registered `InMemorySink` (cached at registration) or `None`.

### 4.2 `BZ2PickleSink` *(deprecated)*

Legacy per-agent disk path. Accepts events only. On
`on_simulation_end()`, writes one `<agent_name>.bz2` file per agent
in the legacy format: a DataFrame indexed by `EventTime` with columns
`EventType` and `Event`. Respects `agent.log_to_file=False` — agents
with the flag cleared produce no disk file. Constructed with
`(log_writer, agents)`.

Emits a one-shot `DeprecationWarning` per process; replacement is
`ParquetSink` (or any custom EventBus sink).

### 4.3 `MetricsObserverSink`

Accepts metrics only. On each `on_metric()` call, forwards
`(agent_id, agent_type, key, value)` to each `KernelObserver` in the
observer list. Registered automatically when the `Kernel` is
constructed with one or more `observers=`.

### 4.4 `ParquetSink`

Optional columnar persistence sink. Lives in
[`abides_core.sinks.parquet_sink`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/sinks/parquet_sink.py)
and requires the `[parquet]` extra:

```bash
pip install 'abides-ng[parquet]'
```

Accepts all three wire kinds by default; toggle individual kinds via
`accept_events=`, `accept_metrics=`, `accept_book_snapshots=` on the
constructor.

Buffers each emission per `(kind, key)` bucket and flushes to
`<root>/<run_id>/{events,metrics,book_snapshots}/<key>.parquet`:

- **events** — bucketed by `event_type` (one file per schema)
- **metrics** — bucketed by metric `key`
- **book_snapshots** — bucketed by `symbol`

Files are written atomically: each bucket is staged under
`<run_id>/.partial/<kind>/` and finalised with `os.replace`. The
`.partial/` directory is wiped on `on_simulation_start()`, so a
crashed prior run leaves no stale data behind.

Set `checkpoint_every_rows=<int>` to rotate large buckets into
numbered shards (`<key>.<seq_lo>-<seq_hi>.parquet`) instead of one
monolithic file.

**Schema (`BUS_FORMAT_VERSION = "2"`).** Each known `event_type` gets
its own typed Arrow schema built from the corresponding
`PayloadSchema.fields`, so each column has a meaningful name and a
precise Arrow dtype (see `_FIELD_TYPE` in `parquet_sink.py` for the
mapping). The three common columns `(agent_id, agent_type,
sim_time_ns)` appear first, then one column per field, then `seq`.
Unknown event types (those absent from `EVENT_TYPE_SCHEMA`) are
pooled into `__generic__.parquet` with a pickled `payload` column
plus an extra `event_type` column.

The reader rejects files whose `abides.bus_format_version` metadata
does not match the current constant.

**Pickled-field exceptions.** Two field types remain pickled:

- `DEPTH.levels` — typed Arrow list&lt;struct&gt; conversion deferred.
- Book-snapshot `bids` / `asks` — same reason.

`unpickle_payloads(df, column)` materialises a pickled binary column;
it is a no-op when `column` is absent.

**Reader.** `read_parquet_logs(run_dir)` walks the on-disk layout,
groups shards back into their logical bucket, validates file metadata,
and returns `dict[str, dict[str, pd.DataFrame]]` keyed by
`{events, metrics, book_snapshots} → bucket_key → DataFrame`, sorted
by `(sim_time_ns, seq)`.

### 4.5 `OrderBookSnapshotMemorySink` and `OrderBookHistoryMemorySink`

Per-symbol sinks that absorb the `OrderBook` capture flow. See §6.

---

## 5. Custom sinks

Pass `event_sinks: list[EventSink]` to `Kernel(...)` to replace **all**
default sinks. Pass `event_sinks=[]` to disable all sinks (no-op
mode). Each sink is registered via `bus.register(sink)` and must
implement the `EventSink` Protocol.

```python
from abides_core.engine.kernel import Kernel
from abides_core.sinks.event_sinks import EventSink, InMemorySink

class CountingSink:
    accept_events = True
    accept_metrics = False
    accept_book_snapshots = False

    def __init__(self) -> None:
        self.n = 0

    def on_simulation_start(self, meta: dict) -> None: ...
    def on_event(self, t: tuple) -> None:
        self.n += 1
    def on_metric(self, t: tuple) -> None: ...
    def on_book_snapshot(self, t: tuple) -> None: ...
    def flush(self) -> None: ...
    def on_simulation_end(self, meta: dict) -> None: ...

kernel = Kernel(..., event_sinks=[InMemorySink(), CountingSink()])
```

---

## 6. `OrderBook` capture on the bus

`OrderBook` no longer owns capture state. Snapshot and event writes
flow through the same `EventBus` used for agent events and metrics,
and are absorbed by per-symbol sinks installed at compile time.

### 6.1 The `book_capture` config field

`ExchangeAgent` takes
`book_capture: Literal["off", "l1", "l2"] | None`. When `None`, it
falls back to the legacy `book_logging` boolean (`True → "l2"`,
`False → "off"`).

| Value | Snapshot publish behaviour | Snapshot sink registered? |
|---|---|---|
| `"off"` | `_publish_snapshot` returns immediately. No bus traffic. | No |
| `"l1"` | Top-of-book only, with publisher-side dedup: skip publish when `(bid_top, ask_top)` is unchanged. | Yes (depth 1) |
| `"l2"` | Full `stream_history` depth. Byte-equivalent to the legacy `book_logging=True` path. | Yes (depth `stream_history`) |

The history sink is **always** registered, regardless of
`book_capture`, because `ExchangeAgent._handle_query_order_stream`
answers `QueryOrderStreamMsg` from it.

### 6.2 Sinks

In [`abides_core.sinks.event_sinks`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/sinks/event_sinks.py):

- **`OrderBookSnapshotMemorySink(symbol, depth)`** —
  `accept_book_snapshots = True`. Filters incoming snapshots by
  `symbol`. Stores parallel column arrays (`times`, `bids`, `asks`).
  `as_book_log2()` returns the legacy `{"QuoteTime", "bids", "asks"}`
  list-of-dicts shape.

- **`OrderBookHistoryMemorySink(symbol)`** — `accept_events = True`.
  Filters by `event_type in BOOK_EVENT_TYPES` and by payload
  `.symbol == symbol`. Stores payload `NamedTuple`s.
  `as_history_dicts()` returns the legacy
  `{"time", "type", **payload_fields}` shape (with `symbol`
  stripped).

One pair is registered per symbol.

### 6.3 Book event vocabulary

Six bare-string event types are published by `OrderBook`, defined in
[`abides_markets.book_events`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/book_events.py):

| `event_type` | `NamedTuple` payload | Fired at |
|---|---|---|
| `LIMIT` | `LimitPayload(symbol, order_id, agent_id, side, quantity, price)` | Every accepted limit order. |
| `EXEC` | `ExecPayload(symbol, order_id, agent_id, oppos_order_id, oppos_agent_id, side, quantity, price)` | Each fill (one per side per match). |
| `CANCEL` | `CancelPayload(symbol, order_id, tag, metadata)` | Full cancel. |
| `CANCEL_PARTIAL` | `CancelPartialPayload(symbol, order_id, quantity, tag, metadata)` | Partial cancel. |
| `MODIFY` | `ModifyPayload(symbol, order_id, new_side, new_quantity)` | In-place quantity / side modify. |
| `REPLACE` | `ReplacePayload(symbol, old_order_id, new_order_id, quantity, price)` | Cancel-and-replace. |

`symbol` is the first field of every payload so a single history
sink can demultiplex events from a multi-symbol exchange. Bare strings
match the historical `history["type"]` literals — zero migration for
consumers that switch from reading `OrderBook.history` to walking the
sink.

`BOOK_EVENT_PAYLOAD_CLASSES` maps event types to their `NamedTuple`
classes.

### 6.4 Reproducibility

With `book_capture="l2"` and a fixed seed, all
`SimulationResult.markets[symbol]` fields are byte-equivalent to a
pre-EventBus baseline pickled at
`abides-markets/tests/data/book_capture_baseline_l2.pkl` (asserted by
`test_book_capture_reproducibility::test_l2_byte_equivalent`). With
`book_capture="l1"`, the L1 series equals the L2 series after
consecutive-duplicate removal (with the empty initial snapshot
dropped), and the L2 series is empty.

---

## 7. `Agent.logEvent` — the producer side

```python
def logEvent(
    self,
    event_type: str,
    event: Any = "",
    append_summary_log: bool = False,   # deprecated
    deepcopy_event: bool = False,
) -> None:
```

Publishes one event tuple on `self.kernel.event_bus`. Cheap: every
in-tree call site uses a registered `event_type` from
`EVENT_TYPE_SCHEMA`, so the payload is either an empty tuple, a bare
scalar, or a pre-built positional tuple (no allocation in the hot
path).

Two flags:

- `deepcopy_event=True` — copy the payload before publishing, so later
  mutation of the original (e.g. a holdings dict) does not poison the
  historical entry. Default `False` for performance.
- `append_summary_log=True` — **deprecated**. Also pushes to
  `Kernel.summary_log`. Emits a one-shot `DeprecationWarning`.

Two control flags on `Agent` itself:

- `log_events: bool = True` — if `False`, `logEvent()` returns
  immediately. Nothing reaches the bus.
- `log_to_file: bool = True` — interpreted by `BZ2PickleSink` only:
  agents with the flag cleared produce no `.bz2` file. `InMemorySink`
  ignores it.

Both are per-agent-instance flags. The declarative config exposes a
`log_orders` override per agent.

---

## 8. Reading the event stream

### 8.1 In-process: `parse_logs_df`

[`abides_core.utils.parse_logs_df`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/utils.py)
is the canonical reader. It walks `end_state["agents"]`, reads each
agent's events from `InMemorySink.agent_log(agent_id)`, flattens the
payload (positional-tuple → dict via the registered schema; dict
passthrough; scalar → `{field_name: value}`), adds `agent_id` and
`agent_type` columns, and concatenates into a single `pd.DataFrame`.

Called from:

- [`abides_markets.simulation.runner`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/simulation/runner.py),
  which populates `SimulationResult.logs` when the requested
  `ResultProfile` includes agent logs.
- Notebook examples and the [data-extraction guide](data-extraction.md).

The metrics system in
[`abides_markets.simulation.metrics`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/simulation/metrics.py)
consumes the parsed DataFrame, not the raw event tuples.

### 8.2 On disk: `read_parquet_logs`

`abides_core.sinks.parquet_sink.read_parquet_logs(run_dir)` is the
canonical reader for `ParquetSink` output. See §4.4.

### 8.3 Deprecated: `Agent.log` property

`agent.log` is a deprecation shim that materialises
`kernel.event_bus.in_memory_sink.agent_log(agent_id)` on every access
and emits a `DeprecationWarning`. Migrate to the explicit sink call
or to `SimulationResult.logs`.

---

## 9. `summary_log` — deprecated

`Kernel.append_summary_log` and
`Agent.logEvent(append_summary_log=True)` are deprecated and pending
removal. The path was a centralised "final state" log inherited from
upstream JPMorgan ABIDES whose intended consumer ("separate
statistical summary programs" — see the upstream `Kernel.__init__`
comment) was never open-sourced; in-process consumers in this fork
standardised on the per-agent path via `parse_logs_df`.

Replacement is `MetricsObserverSink` (or any custom `EventSink`) on
`Kernel.event_bus`. Removal timeline tracked in the
[Deprecated section of CHANGELOG.md](../changelog.md#deprecated).

Each surface emits a one-shot `DeprecationWarning` per process:

| Surface | Replacement |
|---|---|
| `BZ2PickleLogWriter` | `ParquetSink` |
| `BZ2PickleSink` | `ParquetSink` |
| `Agent.logEvent(append_summary_log=True)` | `MetricsObserverSink` |
| `Kernel.append_summary_log` | `MetricsObserverSink` |
| `Agent.log` property | `kernel.event_bus.in_memory_sink.agent_log(agent_id)` |
| `OrderBook.book_log2`, `OrderBook.history` | per-symbol bus sinks (§6.2) |

---

## 10. Filesystem layout

A run with disk persistence produces a directory `<log_root>/<run_id>/`
containing:

```
<log_root>/<run_id>/
├── summary_log.bz2                    # deprecated; pending removal
├── ExchangeAgent0.bz2                 # legacy per-agent BZ2PickleSink output
├── NoiseAgent1.bz2                    # one file per agent with log_to_file=True
├── ValueAgent2.bz2
├── fundamental_<symbol>.bz2           # ad-hoc artefacts via custom filename
└── events/                            # ParquetSink output (if registered)
    ├── ORDER_ACCEPTED.parquet
    ├── ...
    └── __generic__.parquet
```

`log_root` defaults to `"./log"` (created lazily on the first write)
and is passed to `Kernel(log_root=...)` by the compile path from
`SimulationMeta.log_root`. `log_dir` defaults to `uuid.uuid4().hex`,
which avoids wall-clock collisions under multiprocessing.

There is no `simulation.log` for stdout — Python logging output goes
to stdout/stderr only.

---

## 11. Configuration flags

| Flag | Defined in | Default | Controls | Subsystem |
|---|---|---|---|---|
| `Kernel.skip_log` | [`engine/kernel.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/engine/kernel.py) | `True` | Suppress disk writes for the legacy per-agent path and `summary_log` | EventBus disk sinks |
| `Kernel.log_dir` | [`engine/kernel.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/engine/kernel.py) | `uuid.uuid4().hex` | Subdirectory under `log_root` | EventBus disk sinks |
| `Kernel.log_root` | [`engine/kernel.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/engine/kernel.py) | `"./log"` (via `BZ2PickleLogWriter`) | Filesystem root for the legacy per-agent path | EventBus disk sinks |
| `Agent.log_events` | [`agent.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py) | `True` | Whether `logEvent()` publishes at all | EventBus event stream |
| `Agent.log_to_file` | [`agent.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py) | `True` | Whether `BZ2PickleSink` writes the agent's `.bz2` file | Legacy per-agent path |
| `SimulationConfig.simulation.log_level` | [`config_system/models.py`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/config_system/models.py) | `"INFO"` | `basicConfig(level=...)` for stdout | Python logging |
| `SimulationMeta.log_orders` (per agent) | config system | varies | `log_events` / `log_to_file` override per agent type | EventBus event stream |
| `ExchangeAgent.book_capture` | config system | `None` (falls back to `book_logging`) | `OrderBook` snapshot flow (§6.1) | EventBus book snapshots |

The declarative config exposes `log_level` (Python logging) and
`log_orders` overrides (event stream). `skip_log`, `log_dir`, and
`log_root` are reachable only via direct `Kernel(...)` construction
or via `SimulationMeta` plumbing.

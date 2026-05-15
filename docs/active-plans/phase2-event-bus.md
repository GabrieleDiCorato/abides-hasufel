# Phase 2 — Event Bus & Sink Protocol

**Status:** In progress  
**Parent plan:** `docs/project/event-logging-refactor-plan.md` §7 Phase 2  
**Companion:** `docs/reference/event-vocabulary.md` (Phase 0 deliverable)

---

## Autonomous decisions

Three open questions resolved without user input:

| Question | Decision | Rationale |
|---|---|---|
| Payload migration scope | **Bus-first: keep existing payload shapes** (dicts from `order.to_dict()`, etc.). Payload tuple migration deferred to Phase 2a. | Reduces blast radius. The bus is additive; payload shape change is a separate concern. Keeps full logical equivalence trivially. |
| `InMemorySink` storage | **Flat `list[tuple]`** (one per kind). Columnar `array.array` upgrade deferred to Phase 3 alongside `ParquetSink`. | Simpler and correct. The perf win of columnar comes with columnar construction (`from_records` already shipped in Phase 1). |
| BZ2 compat bridge | **Moot for Phase 2**: since payloads stay dicts, `BZ2PickleSink` reconstructs `(EventTime, EventType, Event)` rows from the bus-captured events. Same DataFrame as before. |  |

---

## What changes

### New files in `abides-core/abides_core/`

| File | Contents |
|---|---|
| `event_payloads.py` | `PayloadSchema` dataclass + `EVENT_TYPE_SCHEMA` dict mapping every shipped event type to a schema. No runtime allocation — schemas are module-level singletons. |
| `event_records.py` | `WIRE_FIELDS_*` constants (public API per §3.10). `EventRecord`, `MetricRecord`, `BookSnapshotRecord` typed views with `from_tuple()` classmethods. |
| `event_sinks.py` | `EventSink` Protocol + `InMemorySink`, `BZ2PickleSink`, `MetricsObserverSink`. |
| `event_bus.py` | `EventBus` class: pre-bound no-op, ring buffers, per-handler drain, lifecycle hooks. |

### Modified files

| File | Change |
|---|---|
| `abides_core/agent.py` | `logEvent()` → `bus.publish_event()`; `report_metric()` → `bus.publish_metric()`. Pre-init buffer for events before kernel attach. `Agent.log` → `DeprecationWarning` cached property. Remove DataFrame build + `write_log()` call from `kernel_terminating()`. |
| `abides_core/kernel.py` | Accept `event_sinks` list; build and own `EventBus`. Call `bus.start()` in `initialize()`, `bus.drain()` after each handler, `bus.shutdown()` in `terminate()`. Remove `append_summary_log` dispatch from `observers` (moved to `MetricsObserverSink`). |
| `abides_core/__init__.py` | Export new public symbols. |

### Deferred

- `Order.to_payload_tuple()` and payload tuple migration → Phase 2a
- `HOLDINGS_UPDATED` snapshot → delta schema → Phase 2a
- Columnar `InMemorySink` → Phase 3
- Order-book bus integration → Phase 3a
- `ParquetSink` → Phase 3

---

## Architecture decisions

### Pre-init event buffer

`Agent.__init__` calls `self.logEvent("AGENT_TYPE", type)` before `kernel_initializing()` attaches the kernel. Solution:

```python
# In Agent.__init__
self._pre_init_log: list[tuple[str, Any]] = []  # (event_type, payload)

# logEvent() — before kernel attached
if self.kernel is _UNINITIALIZED_KERNEL:
    self._pre_init_log.append((event_type, event))
else:
    self.kernel.event_bus.publish_event(...)

# kernel_initializing()
for et, ev in self._pre_init_log:
    kernel.event_bus.publish_event(self.id, self.type, 0, et, ev)
self._pre_init_log.clear()
```

Time `0` is used for pre-init events (they carry no meaningful sim time). These are delivered before the first `drain()` call because the bus hasn't started yet; they are flushed at `bus.start()` via a one-shot pre-start buffer.

Actually, cleaner: the bus has a `_pre_start_queue` list. Agent-side `publish_event` calls during construction go into it. At `bus.start()`, pre-start events are drained to all sinks once. After that, the ring buffer takes over.

### Ring buffer + drain cadence

The bus appends to per-kind Python `list`s (cleared in `drain()`). Each list holds events from one handler invocation; typically 1–10 entries. `Kernel.runner()` calls `bus.drain()` once per message dispatch (after wakeup or receive_message returns).

For gym reset loops (`Kernel.reset()` → `initialize()` → `runner()`), `bus.start()` is called at `initialize()` time. The bus must support being started multiple times (gym resets re-use the same `Kernel` instance). At each `bus.start()`, per-kind ring buffers and failed-sink sets are cleared; the pre-start queue is re-drained.

### No-op rebind

At `bus.start()`, per kind:
```python
if not any(s.accept_events for s in self._active_event_sinks):
    self.publish_event = _noop_publish_event  # module-level typed zero-body fn
```
The bus instance attribute shadows the class method; no `self` binding overhead on the call.

### `Agent.log` deprecated cached property

```python
@property
def log(self) -> list[tuple[NanosecondTime, str, Any]]:
    import warnings
    warnings.warn(
        "Agent.log is deprecated. Use kernel.event_bus.in_memory_sink "
        "or SimulationResult.logs instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return self.kernel.event_bus.in_memory_sink.agent_log(self.id)
```

`agent_log(agent_id)` returns a list of `(sim_time_ns, event_type, payload)` tuples for that agent (already in publication order by `seq`). Shape identical to the old `self.log` list.

### `BZ2PickleSink`

Groups events by `agent_id` in an `on_event` dict. At `on_simulation_end`, builds per-agent DataFrames as `(EventTime, EventType, Event)` (with `EventTime` as index) and calls `log_writer.write_agent_log()`. Handles the summary log by flushing metric events from its summary accumulator.

Also rewrites `Kernel.write_summary_log()` to be a no-op if a `BZ2PickleSink` is registered (sink handles it). The summary log DF is built from captured `append_summary_log=True` events (still forwarded from `logEvent` for the deprecation window).

Actually: simplest approach for the deprecation window is to keep `Kernel.append_summary_log()` and the `BZ2PickleLogWriter.write_summary_log()` path UNCHANGED. The bus-based `BZ2PickleSink` only needs to write per-agent logs; summary log continues as before. This avoids touching more surface area.

### Sink failure policy

Failed sinks: caught per-sink in `drain()`, logged as ERROR, sink removed from active set. Failures collected and surfaced at `shutdown()` as a `RuntimeError` listing all failed sinks (default kernel policy = fail-fast at terminate). All other sinks continue to receive events.

### Default sink configuration

When `Kernel` is constructed with `skip_log=False`, default sinks are:
```python
[InMemorySink(), BZ2PickleSink(log_writer, agents), MetricsObserverSink(observers)]
```

When `skip_log=True`, default sinks are:
```python
[InMemorySink()]  # in-memory capture still works; no disk write
```

`event_sinks=[]` (explicit empty list) disables all sinks including `InMemorySink`. In this case all `publish_*` rebind to no-ops.

External callers that pass `event_sinks=` explicitly override the defaults entirely.

### `InMemorySink` public API

```python
class InMemorySink:
    # accept_event_types = None (all), accept_metric_keys = None (all)

    def agent_log(self, agent_id: int) -> list[tuple[NanosecondTime, str, Any]]:
        """Old-style (time, event_type, payload) list for one agent."""

    def to_dataframe(self) -> pd.DataFrame:
        """Full event log as DataFrame. Calls parse_logs_df-compatible shape."""

    @property
    def events(self) -> list[tuple]:
        """Raw wire tuples: (agent_id, agent_type, sim_time_ns, event_type, payload, seq)."""
```

---

## Step list

1. Write `event_payloads.py` — `PayloadSchema` + `EVENT_TYPE_SCHEMA`
2. Write `event_records.py` — wire field constants + typed views
3. Write `event_sinks.py` — `EventSink` Protocol + `InMemorySink` + `BZ2PickleSink` + `MetricsObserverSink`
4. Write `event_bus.py` — `EventBus` with pre-bound no-op, ring buffer, `drain()`, `start()`, `flush()`, `shutdown()`
5. Wire bus into `Kernel`: add `event_sinks` param, create bus, call lifecycle methods
6. Refactor `Agent.logEvent` and `Agent.report_metric`, add `_pre_init_log`, deprecate `Agent.log`
7. Update `abides_core/__init__.py` exports
8. Write logical-equivalence test (fixed-seed small sim, compare per-agent rows)
9. Pre-commit + commit
10. Update `docs/reference/logging-architecture.md`, `CHANGELOG.md`, delete plan

---

## Acceptance gate (from Phase 0 benchmarks)

After Phase 2 lands, re-run the benchmark scripts on the same machine:

- `headless_sim_throughput_no_sinks`: baseline 1.69 s, target **≥ 2.54 s** (1.5×). With no sinks, `publish_*` is a no-op — achieves the goal.
- `gym_episode_throughput_no_sinks`: baseline 0.98 s, target **no regression**. Same no-op path.
- `single_agent_run_with_default_sinks`: baseline 2.42 s, target **≤ 2.42 s** (no regression). The bus drain per handler is a list-append + list-walk; overhead should be < 5%.

Record updated baselines in the jsonl files after Phase 2 merges.

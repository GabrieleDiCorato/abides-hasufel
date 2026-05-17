# ABIDES Logging — Architecture Reference

**Status:** Architecture reference.
**Scope:** Every form of "logging" present in `abides-core`, `abides-markets`,
`abides-gym`. The standard Python `logging` module, the per-agent
`Agent.logEvent()` system, the centralized `summary_log`, and how each
flows through to disk and downstream consumers.
**Audience:** A developer who needs the full picture before making changes.

---

## 0. Executive summary — three independent systems, one bus

ABIDES has **three logging subsystems** that share the name "logging" but
do completely different things and barely interact:

| System | What it logs | Where it goes | Who reads it |
|---|---|---|---|
| **Python `logging`** | Lifecycle, periodic stats, debug traces | stdout (via `basicConfig`) | Operator watching the console |
| **Per-agent event log** (`Agent.logEvent`) | Every business event the agent emits | `EventBus` → registered `EventSink` implementations → `InMemorySink` (in-memory) and/or `BZ2PickleSink` (disk) | `InMemorySink.agent_log()`, `parse_logs_df()`, notebooks, metrics |
| **Summary log** (`Kernel.append_summary_log`) | A handful of "important" events (cash, holdings, valuation) | In-memory `kernel.summary_log` list → `./log/<run_id>/summary_log.bz2` | **Nobody in this fork.** Originally intended for "separate statistical summary programs". |

The per-agent event log flows through the `EventBus`
rather than being stored directly on `agent.log`. See [§4](#4-eventbus-architecture).

### 0.1 Deprecation status (Phase 5 of the event-logging refactor)

The legacy bzip2-pickle path and the `summary_log` opt-in are in the
deprecation window. Each surface emits a `DeprecationWarning` once per
process on first use:

| Surface | Replacement |
|---|---|
| `BZ2PickleLogWriter` (legacy on-disk format) | `abides_core.parquet_sink.ParquetSink` or any EventBus sink |
| `BZ2PickleSink` (legacy event sink) | `ParquetSink` or any EventBus sink |
| `Agent.logEvent(append_summary_log=True)` | `MetricsObserverSink` (or any custom `EventSink`) |
| `Kernel.append_summary_log` | `MetricsObserverSink` (or any custom `EventSink`) |

Full timeline in
[`docs/active-plans/event-logging-refactor-plan.md`](../active-plans/event-logging-refactor-plan.md) §5.

---

## 1. System A — Python `logging` module

### 1.1 Loggers

Every module follows the standard `logger = logging.getLogger(__name__)`
pattern. **18 named loggers** across the three packages
([abides-core/abides_core/kernel.py L17](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L17),
[abides-core/abides_core/agent.py L15](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L15),
oracles, every agent type, `order_book.py`, etc.).

Two CLI entry points use a hardcoded `"abides"` name instead:
[abides-core/abides_core/abides.py L17](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/abides.py#L17)
and [abides-core/scripts/abides L18](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/scripts/abides#L18).

### 1.2 Configuration

There is exactly one configuration call, replicated in three places:

```python
logging.basicConfig(
    level=config["stdout_log_level"],
    format="[%(process)d] %(levelname)s %(name)s %(message)s",
)
```

Sites: [abides.py L47-50](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/abides.py#L47),
[abides.py L150-153](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/abides.py#L150),
[scripts/abides L95-98](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/scripts/abides#L95).

`stdout_log_level` is plumbed from `SimulationConfig.simulation.log_level`
(default `"INFO"`, one of `DEBUG/INFO/WARNING/ERROR/CRITICAL`).

**No `FileHandler`, no `RotatingFileHandler`, nothing else is attached
by ABIDES.** Standard Python logging output goes to stdout/stderr only.
The kernel does not write a `simulation.log` file anywhere.

### 1.3 What the kernel logs

Categorized by purpose ([kernel.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py)):

- **Lifecycle (DEBUG):** `Kernel initialized`, `Kernel started`,
  `Agent.kernel_initializing/starting/stopping/terminating`,
  `Kernel Event Queue begins/empty`. Useful for tracing setup, mostly
  silent at INFO.
- **Periodic checkpoint (INFO):** Every 100,000 messages,
  [kernel.py L325](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L325)
  emits a one-line snapshot:
  `--- Simulation time: ..., messages processed: ..., wallclock elapsed: ...s ---`.
  This is the only INFO output during the hot loop. For a 7-million
  message sim this fires ~70 times.
- **Trace (DEBUG):** Per-pop, per-dispatch, per-requeue debug lines.
  ~6 sites in `runner()` and `_enqueue()`. Gated by
  `logger.isEnabledFor(logging.DEBUG)` so the cost is one cheap branch
  when DEBUG is off. Very expensive when on.
- **Termination summary (INFO):** Event-queue elapsed, msgs/sec,
  per-agent-type mean ending value (the financial leak — see system C),
  `Simulation ending!`.

### 1.4 Trace logging

Trace lines inside the hot loop are emitted via standard
`logger.debug()` calls, gated by
`logger.isEnabledFor(logging.DEBUG)`. To enable them, set the
`abides_core.kernel` logger to `DEBUG`:

```python
import logging
logging.getLogger("abides_core.kernel").setLevel(logging.DEBUG)
```

There is no Kernel attribute toggle.

### 1.5 Verdict on system A

Mostly fine. Standard, predictable, well-behaved Python logging. Two
gaps:

- No way to send stdout logs to a file alongside the per-agent `.bz2`
  files. Documentation in
  [parallel-simulation.md L234](parallel-simulation.md)
  shows users how to attach a `FileHandler` themselves.

---

## 2. System B — per-agent event log (`Agent.logEvent`)

This is the **real** log: the per-event business record that downstream
analytics consume. Don't confuse it with system A.

> **See also:** [`event-vocabulary.md`](event-vocabulary.md) — full
> source-anchored inventory of every shipped `event_type`, payload
> shape, and known consumer.

### 2.1 Method

[agent.py L137-174](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L137):

```python
def logEvent(
    self,
    event_type: str,
    event: Any = "",
    append_summary_log: bool = False,
    deepcopy_event: bool = False,
) -> None:
```

Each agent owns `self.log: list[tuple[NanosecondTime, str, Any]]`
([agent.py L68](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L68)). On every
`logEvent` call, a tuple `(current_time, event_type, event)` is appended.

Two flags:
- `deepcopy_event=True` — copy the event payload before storing, so
  later mutation of the original dict doesn't poison historical
  entries. Default `False` for performance. Used at 5 call sites
  where holdings dicts are logged
  ([trading_agent.py L288, 1148, 1241, 1268, 1298](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L288)
  + [noise_agent.py L124, 132](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/noise_agent.py#L124)
  + [value_agent.py L120](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/value_agent.py#L120)).
- `append_summary_log=True` — also push to the kernel's central
  summary list (system C). Used at the same handful of "final state"
  call sites.

### 2.2 Event vocabulary (~25 unique types)

**Core lifecycle:** `AGENT_TYPE`.
**Cash & holdings:** `STARTING_CASH`, `ENDING_CASH`,
`FINAL_CASH_POSITION`, `FINAL_VALUATION`, `HOLDINGS_UPDATED`,
`MARKED_TO_MARKET`.
**Order submission:** `ORDER_SUBMITTED`, `STOP_ORDER_SUBMITTED`,
`ORDER_ACCEPTED`, `STOP_ORDER_ACCEPTED`.
**Order execution & cancellation:** `ORDER_EXECUTED`, `ORDER_CANCELLED`,
`PARTIAL_CANCELLED`, `CANCEL_SUBMITTED`, `CANCEL_PARTIAL_ORDER`.
**Order modification:** `MODIFY_ORDER`, `ORDER_MODIFIED`,
`REPLACE_ORDER`.
**Market data:** `BID_DEPTH`, `ASK_DEPTH`, `LAST_TRADE`,
`STOP_TRIGGERED`, `MKT_CLOSED`.
**Exchange:** raw `Message.type()` strings — exchange logs every
incoming message at
[exchange_agent.py L420](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/exchange_agent.py#L420).
**Execution algos:** custom strings from `BaseExecutionAgent` and
subclasses.

### 2.3 Disk persistence

Per-agent: at termination, each agent's `self.log` is converted to a
DataFrame `(EventTime, EventType, Event)` indexed by `EventTime`
([agent.py L137-138](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L137)),
then handed to
[kernel.py write_log() L713-753](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L713).

`write_log()`:
- Skips if `self.skip_log`.
- Builds path `./log/<log_dir>/<agent_name_no_spaces>.bz2`.
- Calls `df.to_pickle(path, compression="bz2")`.

Both the path and the format are hardcoded. The kernel decides
filesystem layout, choice of pickle, choice of bz2.

A handful of agents call `write_log()` again with a custom `filename` for
extra artifacts — e.g.
[exchange_agent.py L327](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/exchange_agent.py#L327)
writes `fundamental_<symbol>.bz2`.

### 2.4 Two control flags on `Agent` itself

- `log_events: bool = True` ([agent.py L33](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L33))
  — if `False`, `logEvent()` becomes a no-op. The agent records
  nothing.
- `log_to_file: bool = True` ([agent.py L34](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L34))
  — if `False`, the agent's log stays in memory and is never written
  to disk (but is still exposed via `agent.log`).

These are **per-agent-instance** flags. There is no global way to
"disable order logs across all agents". Configs that want to suppress
order logs for, say, noise agents must set the flag per-agent-type at
build time. The config system has helpers (`log_orders` override at
[test_config_system.py L1179](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/tests/test_config_system.py#L1179)).

### 2.5 Downstream: `parse_logs_df`

[abides-core/abides_core/utils.py L154-186](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/utils.py#L154):

```python
def parse_logs_df(end_state: dict) -> pd.DataFrame:
    # iterate end_state["agents"], walk each agent.log,
    # flatten the Event payload (dict-expanded if dict),
    # add agent_id and agent_type columns,
    # concat one row at a time into a single DataFrame.
```

This is the **canonical reader** of system B. It is called from:

- [abides-markets/abides_markets/simulation/runner.py L349](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/simulation/runner.py#L349)
  — populates `SimulationResult.logs` when the requested `ResultProfile`
  includes agent logs.
- [data-extraction.md](data-extraction.md) — the
  documented public API for users.
- Notebook examples (e.g. `demo_ABIDES-Markets.ipynb`).

`parse_logs_df` operates on **in-memory `agent.log` lists**, not on the
written `.bz2` files. The `.bz2` files are an export artifact, not the
runtime data path.

The metrics system in
[abides-markets/abides_markets/simulation/metrics.py](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/simulation/metrics.py)
consumes the *parsed* DataFrame, not the raw logs.

### 2.6 Performance and memory

- **Per-event allocation:** every `logEvent` call appends a 3-tuple.
  Cheap. `deepcopy_event=True` is more expensive but rare.
- **End-of-simulation conversion:** `pd.DataFrame(...)` over
  potentially millions of rows, then `to_pickle(compression="bz2")`.
  Both serial, both slow for large sims. No incremental flushing. No
  streaming format.
- **`parse_logs_df` builds one DataFrame per agent then concats** —
  was O(N) but allocated an intermediate frame for every agent. Now
  rebuilt around a single `pd.DataFrame.from_records` over the flat
  row list, eliminating the per-agent allocation. A further
  optimisation that builds the DataFrame directly from `InMemorySink`
  column arrays is still pending.

### 2.7 Verdict on system B

This is the **load-bearing** logging system. Everything downstream
(metrics, plots, replay tooling) depends on it. It works, but has three
real issues:

1. **Format is fused into the kernel.** No way to swap pickle for
   parquet, no way to mock the writer for tests.
2. **`parse_logs_df` allocates an intermediate DataFrame per agent.**
   The single `from_records` rewrite eliminated the per-agent frame; a
   deeper rewrite over `InMemorySink` columnar arrays remains a
   pending optimisation.
3. **`log_events` / `log_to_file` are per-instance**, awkward to set
   globally.

---

## 3. System C — `summary_log` (the centralized one)

### 3.1 The mechanism

[kernel.py L87](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L87): a list
populated by
[`append_summary_log()`](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L755-772):

```python
def append_summary_log(self, sender_id, event_type, event):
    self.summary_log.append({
        "AgentID": sender_id,
        "AgentStrategy": self.agents[sender_id].type,
        "EventType": event_type,
        "Event": event,
    })
```

Triggered from [agent.py L172-174](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L172)
*only* when the agent passes `append_summary_log=True` to `logEvent`.

### 3.2 Who actually uses it

Verified by grep — only **7 call sites** pass `append_summary_log=True`:

| Event | Caller |
|---|---|
| `STARTING_CASH` | [trading_agent.py L233](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L233) |
| `FINAL_CASH_POSITION` | [trading_agent.py L256](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L256) |
| `ENDING_CASH` | [trading_agent.py L261](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L261) |
| `HOLDINGS_UPDATED` | [trading_agent.py L288](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/trading_agent.py#L288) |
| `FINAL_VALUATION` | [noise_agent.py L124, 132](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/noise_agent.py#L124) |
| `FINAL_VALUATION` | [value_agent.py L120](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/agents/value_agent.py#L120) |

These are all **end-of-day financial summary events**. The intent
(judging from the call sites) was to give downstream tools a fast path
to "the final state of everyone's books" without scanning every agent's
full log.

### 3.3 Disk write

[kernel.py write_summary_log() L774-783](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L774):

```python
def write_summary_log(self) -> None:
    path = os.path.join(".", "log", self.log_dir)
    file = "summary_log.bz2"
    if not os.path.exists(path):
        os.makedirs(path)
    df_log = pd.DataFrame(self.summary_log)
    df_log.to_pickle(os.path.join(path, file), compression="bz2")
```

**Bug:** does not honour `skip_log` — writes the file unconditionally.
Already captured as A.1 in the kernel improvement plan.

Called once, from
[`terminate()` at kernel.py L493](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L493).

### 3.4 Who reads it

**Nobody.** Verified by grep:
- No code path opens `summary_log.bz2`.
- No `pd.read_pickle(.*summary)` anywhere.
- No tool, no notebook, no test, no documentation page tells the user
  how to use it.
- The file *is* listed in
  [parallel-simulation.md L315](parallel-simulation.md)
  as part of the on-disk layout, with the description "Kernel summary
  (agent types, final values)" — but no consumer is documented or
  implemented.

### 3.5 Verdict on system C

**Vestigial.** It collects a small subset of system B's data into a
parallel structure, writes it to a file that no internal tool reads,
and survives only because nobody removed it.

If a future feature wants "fast final-state summary", the right path is
to compute it from the (already in-memory) per-agent logs, or to use the
new `report_metric` mechanism planned in B.4 of the kernel improvement
plan. There is no current consumer to break.

---

## 4. Filesystem layout

A run produces a directory `./log/<log_dir>/` containing:

```
./log/<log_dir>/
├── summary_log.bz2                    # System C — written, never read
├── ExchangeAgent0.bz2                 # System B — per-agent log (DataFrame)
├── NoiseAgent1.bz2
├── ValueAgent2.bz2
├── ...                                # one .bz2 per agent that has log_to_file=True
└── fundamental_<symbol>.bz2           # ad-hoc artifacts via custom filename
```

`<log_dir>` defaults to `str(int(wall_clock_seconds))`
([kernel.py L136](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L136)). This
**collides** if two simulations start in the same second. The high-level
`run_simulation()` wrapper avoids this by generating a UUID when
`log_dir is None`; the low-level `Kernel(...)` and CLI do not.

The path root `./log/` is hardcoded
([kernel.py L743, 776](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L743)).
There is no `log_root` parameter; the kernel writes into the current
working directory.

There is no `simulation.log` for stdout — system A goes only to stdout.

---

## 5. Configuration knobs (every flag, one table)

| Flag | Where defined | Default | Controls | System |
|---|---|---|---|---|
| `Kernel.skip_log` | [kernel.py L54, 133](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L54) | `True` | Suppress disk writes for B + C (but see bug 3.3 — C ignores it today) | B + C |
| `Kernel.log_dir` | [kernel.py L56, 136](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L56) | `uuid.uuid4().hex` | Subdirectory under `./log/` | B + C |
| `Agent.log_events` | [agent.py L33](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L33) | `True` | Whether `logEvent()` records anything in memory | B |
| `Agent.log_to_file` | [agent.py L34](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/agent.py#L34) | `True` | Whether the agent's log is written at termination | B |
| `SimulationConfig.simulation.log_level` | [config_system/models.py L404](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/abides_markets/config_system/models.py#L404) | `"INFO"` | `basicConfig(level=...)` for stdout | A |

Notable: the user-facing config system exposes `log_level` (system A)
and `log_orders` overrides per agent (system B), but **does not expose**
`skip_log` or `log_dir`. Those are reachable only by passing them to
`Kernel(...)` directly or by post-construction mutation. Trace logging
is enabled by setting the `abides_core.kernel` logger level to `DEBUG`.

---

## 6. Tests

Tests confirm the load-bearing behaviour but reveal the asymmetry:

- **System A:** no tests. `basicConfig` is fire-and-forget.
- **System B:** rich coverage —
  [test_pandas_integration.py L186-413](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/tests/test_pandas_integration.py#L186)
  covers `parse_logs_df`, end-to-end disk round-trip, type coercion;
  [test_simulation.py L553-572](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/tests/test_simulation.py#L553)
  covers `SimulationResult.logs` shape and presence per profile;
  [test_replace_order_regression.py L344-388](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/tests/test_replace_order_regression.py#L344)
  covers REPLACE/MODIFY/CANCEL log records; config-system tests at
  [test_config_system.py L1169, 1179, 1437](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-markets/tests/test_config_system.py#L1169)
  exercise `log_level()` and `log_orders` overrides.
- **System C:** no tests. No reader, no round-trip check, nothing
  asserts on `summary_log.bz2`.

Most kernel tests construct with `skip_log=True` to avoid touching the
filesystem ([test_kernel.py L47, 54, 63, 83](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/tests/test_kernel.py#L47)).

---

## 7. Issues, smells, and risks (consolidated)

### 7.1 Real correctness bugs

- **`write_summary_log()` ignores `skip_log`**
  ([kernel.py L774-783](https://github.com/GabrieleDiCorato/abides-ng/blob/main/abides-core/abides_core/kernel.py#L774)).
  Fix queued as A.1 in the kernel improvement plan. Severity: low
  (file is unused), but a unit-test surprise.

### 7.2 Real performance issues

- **`parse_logs_df` allocated one intermediate DataFrame per agent.**
  The single `pd.DataFrame.from_records` rewrite eliminated that
  intermediate; a further rewrite over `InMemorySink` columnar arrays
  is still pending.
- **`to_pickle(compression="bz2")`** is the slowest pickle path.
  Acceptable for a one-shot serialization but compounds when many
  agents log a lot.

### 7.3 Architectural smells

- **Kernel owns filesystem.** Path construction, format choice,
  directory creation, error handling were all inside
  `kernel.write_log` / `kernel.write_summary_log`. **Resolved by PR 7**:
  the kernel now delegates to an injectable `LogWriter` Protocol
  (`abides_core.log_writer`); the legacy bz2-pickle path is one
  implementation among several.
- **Hardcoded `./log/`** — the kernel used to write into CWD with no
  override. **Resolved by PR 7**: `Kernel(log_root=...)` is now a
  first-class kwarg, and the run directory is created lazily on the
  first write rather than at construction time.
- **Three logging systems share one `./log/<run_id>/` directory** with
  different lifecycles and consumers. Not separated, not labelled in
  the directory layout. A user looking at the folder cannot tell what
  is what without reading source.
- **`summary_log` is dead code with an externally visible artifact.**
  Kept for one release for safety, but nobody uses it. Either remove
  it or design it properly.
- **`log_events` / `log_to_file` are per-instance, not per-type.**
  Setting them across "all noise agents" requires loop-and-mutate at
  build time.

### 7.4 Robustness

- **No error handling around log writes.** Disk full, permission
  denied, pickle failure → kernel crash at the very end of a run, after
  hours of simulation. No try/except, no temp-file-then-rename.
- **No incremental flushing.** Memory usage grows linearly with event
  count. Not observed as a problem today (most sims < 1M events) but a
  hard ceiling for ABIDES-gym training that runs many episodes.
- **Pickle deserialization is untrusted-input-unsafe.** Loading a
  `.bz2` from a third party can execute arbitrary code. ABIDES does
  not advertise the file as portable, but users do share them.

### 7.5 What is *not* a problem

- Standard Python logging is well-behaved. Lazy formatting in the hot
  loop landed in PR 3.
- The `Agent.logEvent` API is good. Cheap, simple, lossless.
- `parse_logs_df` is the right shape (DataFrame), just implemented
  poorly.
- Test coverage of system B is solid.

---

## 8. The mental model someone should leave with

> **System A** is operator output. It tells you the simulation is alive
> and roughly how fast. It is not a record of the simulation.
>
> **System B** is the simulation's record. Every agent appends to its
> own list at every event; at the end, lists become DataFrames and (if
> not skipped) get pickled to disk. `parse_logs_df` is the official
> reader.
>
> **System C** is dead weight that produces a file nobody reads. It is
> retained only because removing it would break the documented disk
> layout.
>
> The kernel used to entangle all three: it owned the format, the
> filesystem path, the lifecycle, and a financial-summary leak from
> the markets layer. **PR 4 moved financial metrics out of core**, and
> **PR 7 introduced the `LogWriter` Protocol** so the kernel no longer
> owns format or path. The lifecycle is now governed by `KernelState`
> (`abides_core.lifecycle`).

---

## 9. Open questions — what a redesign would have to decide

These are *not* recommendations. They are the choices a redesign cannot
avoid:

1. **Should `summary_log` survive?** Remove (free), keep (needs a
   reader and a purpose), or replace with the new `report_metric()`
   mechanism (B.4 in the kernel plan)?
2. **Format pluggability.** PR 7 provided the seam (`log_writer=`
   kwarg + `LogWriter` Protocol). The *choice* of additional formats
   (parquet, JSONL, sqlite) is still open — only `NullLogWriter` and
   `BZ2PickleLogWriter` ship today.
3. **Incremental writes.** Do long simulations need streaming flush, or
   is "all at terminate" forever good enough?
4. **`./log/` root.** Resolved by PR 7: `Kernel(log_root=...)` is now
   a kwarg with `"./log"` as default. CWD-relative remains the default
   for backwards compatibility.
5. **`log_dir` collision.** Resolved: `Kernel.log_dir` defaults to
   `uuid.uuid4().hex` to avoid wall-clock collisions under
   multiprocessing.
6. **Per-instance vs per-type log flags.** Add a config-system
   convenience for "disable order logs for this agent type globally",
   or accept the loop-and-mutate idiom?
7. **Standard logging to file.** Should ABIDES attach a `FileHandler`
   that writes `simulation.log` next to the per-agent files, or keep
   stdout-only and let users add it themselves?
9. **Pickle vs portable format.** Is the `.bz2` file an internal cache
   (pickle is fine) or an interchange artifact (parquet/arrow is
   safer)?
10. **Error handling on write.** Best-effort write with warning, or
    fail-fast and crash the run?

These are decisions for a separate plan. This document is the picture,
not the prescription.

---

## A. Original intent of `summary_log` (archaeology)

`summary_log` is **not new**. It was inherited verbatim from the
upstream JPMorgan ABIDES public release (commit `3abbd6f` — "ABIDES
public commit") and has not been touched since. The upstream
`Kernel.py` carries the answer in two comments that did not survive the
fork's reformatting.

### A.1 The mission statement (upstream `Kernel.__init__`)

```python
# The Kernel maintains a summary log to which agents can write
# information that should be centralized for very fast access
# by separate statistical summary programs.  Detailed event
# logging should go only to the agent's individual log.  This
# is for things like "final position value" and such.
self.summary_log: List[Dict[str, Any]] = []
```

### A.2 The contract (upstream `append_summary_log` docstring)

```
We don't even include a timestamp, because this log is for
one-time-only summary reporting, like starting cash, or ending cash.

Arguments:
    sender_id: The ID of the agent making the call.
    event_type: The type of the event.
    event:      The event to append to the log.
```

### A.3 What this tells us

The original design carved out a **deliberate two-tier logging split**:

| Tier | Per-agent `agent.log` | Central `summary_log` |
|---|---|---|
| **Granularity** | Every event, with timestamp | One-shot final-state events, no timestamp |
| **Audience** | Per-run diagnostics, replay, microstructure analysis | "Separate statistical summary programs" (i.e. cross-run batch analytics) |
| **Cost** | One file per agent per run | One small file per run |
| **Why centralized** | N/A | A batch tool can `pd.read_pickle` one file per run instead of N agent files, and get a denormalized, ready-to-aggregate table of "everyone's bottom line" |

The intent makes sense for a research workflow that runs many parallel
simulations from a shell script (which is how upstream ABIDES was
operated — `summary_log.bz2` was the **cross-simulation roll-up
artifact**).

### A.4 Why no readers exist in this fork

Three plausible explanations, in decreasing order of likelihood:

1. **The "separate statistical summary programs" were never released.**
   The upstream public repo ships the producer half of the contract
   without the consumer half. The aggregation tool likely existed
   inside JPMorgan and was not open-sourced. The fork inherited an
   API with no reachable consumer.
2. **`parse_logs_df` superseded it in practice.** Once the high-level
   `run_simulation()` wrapper landed and returned a parsed DataFrame
   in memory, downstream code in this fork (notebooks, metrics,
   `SimulationResult`) standardized on **the per-agent path**.
   `summary_log.bz2` became redundant for in-process consumers and
   nobody built the cross-run consumer.
3. **Schema friction.** The summary record has no timestamp and no
   uniform schema for `event` (each event type stuffs a different
   dict shape in there). Even an external tool would need
   per-event-type unpacking logic — at which point reading the
   per-agent logs gives strictly more information for similar effort.

### A.5 Implications for the redesign

This reframes the question from "is this dead code?" to "**is the
two-tier split still the right design?**":

- **The need is real.** Cross-run aggregation ("across these 200 sims,
  what is the distribution of final NoiseAgent valuations?") is a
  legitimate and recurring use case for ABIDES. The kernel improvement
  plan's `report_metric()` mechanism (B.4) is one answer, but it
  aggregates *within* a run — not across runs.
- **The current artifact does not serve it.** No reader, no schema,
  no documented format.
- **Three coherent futures:**
  1. **Remove** `summary_log` and rely on per-agent logs +
     `parse_logs_df` for everything. Cross-run aggregation becomes
     "load N pickle files, concat, group". Simple, slower at scale.
  2. **Repurpose** `summary_log` as the on-disk projection of
     `report_metric()`'s aggregated results. Same artifact name,
     well-defined schema (`agent_type`, `key`, `sum`, `count`, `mean`),
     ready for cross-run batch tools.
  3. **Keep as-is, document as legacy**, and add a `WARNING` log line
     when the file is written so users know it exists.

Option 2 is the most honest: it preserves the original two-tier intent,
gives the artifact a real schema, and reuses the `report_metric()`
infrastructure already planned. It would be a follow-up plan, not part
of the current kernel refactor.

The choice is out of scope for this analysis. What is in scope is the
correction: **`summary_log` is not orphaned because it was
ill-conceived. It is orphaned because the consumer half of the original
contract was never open-sourced.**

---

## 4. EventBus architecture

The `EventBus` replaced the direct `agent.log` list and the direct observer
calls with a single-threaded, per-simulation `EventBus`. This section
is the authoritative reference for the bus architecture.

### 4.1 Key modules

| Module | Purpose |
|--------|---------|
| `abides_core/event_bus.py` | `EventBus` — dispatch hub |
| `abides_core/event_sinks.py` | `EventSink` Protocol + three shipped sinks |
| `abides_core/event_records.py` | Wire field constants and typed record views |
| `abides_core/event_payloads.py` | `PayloadSchema` registry + `EVENT_TYPE_SCHEMA` map |
| `abides_core/parquet_sink.py` | Optional `ParquetSink` + `read_parquet_logs` reader (requires `[parquet]` extra) |

### 4.2 EventSink Protocol

```python
@runtime_checkable
class EventSink(Protocol):
    accept_events: bool
    accept_metrics: bool
    accept_book_snapshots: bool

    def on_simulation_start(self, meta: dict) -> None: ...
    def on_event(self, t: tuple) -> None: ...       # 6-field wire tuple
    def on_metric(self, t: tuple) -> None: ...      # 6-field wire tuple
    def on_book_snapshot(self, t: tuple) -> None: ... # 6-field wire tuple
    def flush(self) -> None: ...
    def on_simulation_end(self, meta: dict) -> None: ...
```

### 4.3 Wire tuple formats

**Event wire tuple** (6 fields, index-ordered):

| Index | Field | Type |
|-------|-------|------|
| 0 | `agent_id` | `int` |
| 1 | `agent_type` | `str` |
| 2 | `sim_time_ns` | `int` (nanoseconds) |
| 3 | `event_type` | `str` |
| 4 | `payload` | `Any` |
| 5 | `seq` | `int` (monotonically increasing) |

Constants: `WIRE_FIELDS_EVENT`, `WIRE_FIELDS_METRIC`, `WIRE_FIELDS_BOOK_SNAPSHOT`.
Typed views: `EventRecord.from_tuple(t)`, `MetricRecord.from_tuple(t)`, `BookSnapshotRecord.from_tuple(t)`.

### 4.3.1 Payload shapes (Phase 2b)

Every `event_type` shipped by in-tree agents is registered in
`abides_core.event_payloads.EVENT_TYPE_SCHEMA`, mapping the string key
to a frozen `PayloadSchema(name, version, fields)`. The `fields` tuple
determines the on-wire payload shape:

| Arity (`len(fields)`) | On-wire payload | Example schema | Example call |
|-----------------------|------------------|----------------|--------------|
| 0 | `EMPTY_PAYLOAD` (the empty tuple `()`) | `EMPTY` | `self.logEvent("MKT_CLOSED", EMPTY_PAYLOAD)` |
| 1 | bare scalar (no tuple wrapping) | `CASH = (cents,)` | `self.logEvent("STARTING_CASH", 10_000_000)` |
| ≥ 2 | positional tuple of length `arity` | `ORDER_EVENT = (...)` | `self.logEvent("ORDER_ACCEPTED", order.to_payload_tuple())` |

The `Order`, `LimitOrder` and `StopOrder` value objects expose
`to_payload_tuple()` returning an `ORDER_EVENT`-shaped tuple
(`Side`/`TimeInForce` are `IntEnum` so they cross the wire as ints;
`Side.legacy_str()` / `TimeInForce.legacy_str()` are provided for
human-readable reporting). The legacy `to_dict()` method is retained
as a deprecation-window wrapper that converts the tuple back to a
dict and will be removed in the Phase 5+2 cleanup.

Dynamic-name events that cannot appear in the static registry are
still bounded:

* `ExchangeAgent` echoes incoming `OrderMsg` subclasses under
  `message.type()` (e.g. `"LimitOrderMsg"`); every such class is
  registered in `EVENT_TYPE_SCHEMA` against `ORDER_EVENT`.
* Non-order ExchangeAgent receipts (query / subscription requests)
  log an `EMPTY` payload under the message class name (allowlist of
  `QueryMsg`, `MarketHoursRequestMsg`, `MarketClosePriceRequestMsg`,
  `MarketDataSubReqMsg`); anything else is warn-and-dropped via the
  stdlib logger so no raw `Message` instance can leak onto the bus.
* `OrderBook` post-only rejections emit dynamic
  `<order.tag>_POST_ONLY` events with a small dict payload; these
  intentionally fall through to the `GENERIC` bucket.

`InMemorySink` enforces both halves of the contract at append time:
unregistered event types are routed to `GENERIC` with a one-time
warning, and rows whose payload does not match the chosen schema's
arity are diverted to a per-type `"<event_type>::generic"` fallback
bucket rather than torn-written into the typed bucket. The
`parse_logs_df()` consumer mirrors the same projection: arity 0 →
`{"EmptyEvent": True}`, arity 1 → `{fields[0]: payload}`, arity ≥ 2 →
`dict(zip(fields, payload))`, dict payloads passed through unchanged.

The build-time invariant is enforced by
`abides-core/tests/test_event_payload_schema.py`, which walks every
`.py` file under `abides-core/`, `abides-markets/` and `abides-gym/`
and inspects calls to `logEvent`, `publish_event`, `publish_metric`
and `publish_book_snapshot`. The test fails if a string-literal
`event_type` is missing from `EVENT_TYPE_SCHEMA` or if the payload
argument is an f-string (`ast.JoinedStr`) or a literal `str(x)` call.

### 4.4 Shipped sinks

**`InMemorySink`** — registered by default when `event_sinks` is not
explicitly passed to `Kernel`. Stores events in a schema-driven
columnar layout (`_cols: dict[event_type, dict[column, list]]`) and
metrics / book snapshots as raw wire-tuple lists. Each event-type
bucket carries the four common wire columns (`agent_id`, `agent_type`,
`sim_time_ns`, `seq`) plus one column per
`PayloadSchema` field; events whose `event_type` is missing from
`EVENT_TYPE_SCHEMA` fall through to a single `payload` column under
the `GENERIC` schema with a one-time warning. Payload shape is
validated against the chosen schema before any column is touched, so
appends are transactional: a mismatched row is diverted to a per-type
`"<event_type>::generic"` fallback bucket rather than leaving a typed
bucket torn. Key API:
- `agent_log(agent_id)` → `list[tuple[int, str, Any]]` — `(sim_time_ns, event_type, payload)` triples, matching the old `agent.log` format. Reconstructed from the columnar buckets and sorted by `seq`.
- `events`, `metrics`, `book_snapshots` — wire tuple lists (`events` is rebuilt on demand from the columns; cache the result if you scan it more than once).
- `columns` → the raw `dict[event_type, dict[column, list]]` mapping for zero-copy analytics paths (e.g. Arrow exporters).
- `bucket_schema(event_type)` → the `PayloadSchema` chosen for a given bucket, or `None`.
- `to_dataframe()` → wide-flat `pd.DataFrame` of all events with `WIRE_FIELDS_EVENT` columns.

**`BZ2PickleSink`** — accepts events only. Writes
`<agent_name>.bz2` files on `on_simulation_end()` in the legacy
format: a DataFrame indexed by `EventTime` with columns `EventType` and
`Event`. Respects `agent.log_to_file=False` — agents with the flag
cleared produce no disk file. Constructed with `(log_writer, agents)`.

**`MetricsObserverSink`** — accepts metrics only. On each `on_metric()`
call, forwards `(agent_id, agent_type, key, value)` to each
`KernelObserver` in the observer list. Replaces the old direct call
from `agent.report_metric()`.

**`ParquetSink`** — optional columnar persistence sink. Accepts all
three wire kinds by default; toggle individual kinds via
`accept_events=`, `accept_metrics=`, `accept_book_snapshots=` on the
constructor. Lives in `abides_core.parquet_sink` and requires the
`[parquet]` extra:

```bash
pip install 'abides-ng[parquet]'
```

Buffers each bus emission per `(kind, key)` bucket and flushes to a
Parquet file at `<root>/<run_id>/{events,metrics,book_snapshots}/<key>.parquet`:

- **events** — bucketed by `event_type` (one file per schema)
- **metrics** — bucketed by metric `key`
- **book_snapshots** — bucketed by `symbol`

Files are written atomically: each bucket is staged under
`<run_id>/.partial/<kind>/` and finalized with `os.replace`. The
`.partial/` directory is wiped on `on_simulation_start()`, so a crashed
prior run leaves no stale data behind. Set
`checkpoint_every_rows=<int>` to rotate large buckets into numbered
shards (`<key>.<seq_lo>-<seq_hi>.parquet`) instead of one monolithic
file; without checkpointing, a single unnumbered file per bucket is
produced.

**Schema (MVP):** Events use 5 columns `(agent_id, agent_type,
sim_time_ns, payload, seq)` with `payload` stored as a pickled binary
blob; book snapshots store `bids`/`asks` the same way. Metrics use
typed `(agent_id, agent_type, sim_time_ns, value: float64, seq)`. The
pickled-payload form is a Phase 3 simplification — Phase 2 payloads
remain heterogeneous Python objects, and typed Arrow columns become a
follow-up once payloads are normalized to tuples. Future schema
migrations will bump `abides.bus_format_version` (currently `"1"`),
recorded as Parquet file metadata along with the schema name/version,
metric key, or symbol. The reader rejects files whose bus format
version does not match.

Unknown event types (those without a registered schema) are pooled into
a single `__generic__.parquet` file with an extra `event_type` column;
one `RuntimeWarning` is emitted per distinct unknown type.

**Reader:** `read_parquet_logs(run_dir)` walks the on-disk layout,
groups shards back into their logical bucket, validates file metadata,
and returns `dict[str, dict[str, pd.DataFrame]]` keyed by
`{events, metrics, book_snapshots} → bucket_key → DataFrame`, sorted
by `(sim_time_ns, seq)`. A companion `unpickle_payloads(df)` helper
materializes the pickled `payload` (or `bids`/`asks`) column into live
Python objects.

**Non-goals for Phase 3:** no background writer thread, no spill-to-disk
backpressure, no `SinkConfig` integration, no typed Arrow columns. The
sink is fully synchronous and writes from the main thread on
`on_simulation_end()` (and at each checkpoint boundary).

### 4.5 Bus lifecycle

```
Kernel.__init__()          → EventBus() created; sinks registered.
Kernel.initialize()        → bus.start(meta)
                              • calls on_simulation_start on all sinks
                              • drains pre-start queue (AGENT_TYPE events etc.)
                              • rebinds publish_* to real or no-op methods
Kernel.runner() per-tick   → bus.drain()   (after each message dispatch)
Kernel.terminate()         → bus.shutdown(meta)
                              • drain() + on_simulation_end on all sinks
                              • rebinds to pre-start stubs (for gym reuse)
```

**Drain cadence:** Once per message dispatch in `runner()`, and once
at `terminate()`. Events are **not** dispatched synchronously on
`logEvent()`. If you read `InMemorySink` data outside the normal
lifecycle (e.g. in tests that call only `initialize()`), call
`kernel.event_bus.drain()` first.

### 4.6 Pre-init bootstrap and `AGENT_TYPE`

`Agent.__init__()` does **not** publish `AGENT_TYPE`.  It only allocates
an empty `_pre_init_log` buffer for any subclass that calls
``logEvent()`` from its own ``__init__``.  Each call to
``Agent.kernel_initializing()`` publishes a fresh
``AGENT_TYPE`` event to the bus it has just attached to, then flushes
the pre-init buffer.  This guarantees ``AGENT_TYPE`` is re-emitted on
every kernel attach (the gym-reset pattern of constructing a new
``Kernel`` with the same agent).  All bootstrap events carry
``sim_time_ns=0``.

Because ``bus.start()`` has not been called yet, these events enter the
pre-start queue and are drained automatically when ``bus.start()`` is
called.  The pre-start queue exists for all three wire kinds
(``publish_event``, ``publish_metric``, ``publish_book_snapshot``), so
book snapshots published before ``start()`` (e.g. by an oracle warm-up
step) are also delivered.

### 4.7 Custom sinks

Pass `event_sinks: list[EventSink]` to `Kernel(...)` to replace all
default sinks. Pass `event_sinks=[]` to disable all sinks (no-op mode).
Each sink is registered with `bus.register(sink)` and must implement
the `EventSink` Protocol.  ``register()`` validates the Protocol at
registration time and raises ``TypeError`` with a missing-method list
if the object does not conform; the ``accept_*`` class attributes are
checked explicitly.

### 4.8 Failure isolation

A single ``try/except`` wraps the whole tuple loop for each sink in
``_drain_buffers()``.  On exception:

- The sink is added to ``_failed_sinks`` and the failure tuple is
  appended to ``_sink_failures`` once (subsequent batches for the same
  sink are skipped entirely).
- The remaining tuples of the *current* batch are dropped for that
  sink only.
- Other sinks see the full batch.
- The exception is logged at ``ERROR`` with ``exc_info``.
- ``bus.shutdown()`` raises ``RuntimeError`` summarising all failed
  sinks; ``Kernel.terminate()`` catches and logs this rather than
  re-raising (conservative current behaviour).
- The failures are also surfaced programmatically on
  ``KernelRunResult.sink_failures`` as a tuple of ``SinkFailure``
  records (``sink_index``, ``sink_type``, ``exception_repr``).  Callers
  that want to fail the run on any sink failure can check this field
  after ``kernel.run()``.

The per-batch (rather than per-tuple) wrapping avoids the overhead of
millions of ``try/except`` frames in the hot dispatch path and prevents
a known-broken sink from being re-invoked for every remaining tuple.

### 4.9 Deprecated `agent.log` property

Accessing `agent.log` emits a `DeprecationWarning` and
returns `InMemorySink.agent_log(agent.id)` (or `[]` if no sink is
registered). Update callers to use
`kernel.event_bus.in_memory_sink.agent_log(agent_id)` directly, or use
`parse_logs_df()` which already reads from the sink.

---

## 5. OrderBook capture on the EventBus

Historically, every `OrderBook` instance owned two per-instance Python
lists: `book_log2` (snapshots of the L2 book after each mutation) and
`history` (a dict per `LIMIT` / `EXEC` / `CANCEL` / `CANCEL_PARTIAL` /
`MODIFY` / `REPLACE` event).  The runner read those lists directly to
produce `SimulationResult.l1_series`, `l2_series`, `trades`, and
`liquidity`.  This coupled storage policy to the producer and forced
every consumer onto the same in-memory format.

Both flows now move onto the same `EventBus` used for
agent events and metrics.

### 5.1 The `book_capture` config field

`ExchangeAgent` now takes
``book_capture: Literal["off","l1","l2"] | None``.  When `None`, it
falls back to the legacy ``book_logging`` boolean (``True → "l2"``,
``False → "off"``) so existing configs keep working.

| Value | Snapshot publish behaviour | Snapshot sink registered? |
|---|---|---|
| `"off"` | `_publish_snapshot` returns immediately. No bus traffic. | No |
| `"l1"` | Top-of-book only, with publisher-side dedup: skip publish when `(bid_top, ask_top)` is unchanged. | Yes (depth=1) |
| `"l2"` | Full `stream_history` depth. Byte-equivalent to the legacy `book_logging=True` path. | Yes (depth=`stream_history`) |

The history sink is **always** registered (regardless of
`book_capture`) because `ExchangeAgent._handle_query_order_stream`
answers `QueryOrderStreamMsg` from it; the legacy `book_logging` flag
never gated history either.

### 5.2 Two new sinks

In `abides_core.event_sinks`:

- **`OrderBookSnapshotMemorySink(symbol, depth)`** — `accept_book_snapshots = True`.
  Filters incoming snapshots by `symbol`.  Stores parallel column
  arrays (`times`, `bids`, `asks`).  Exposes
  `as_book_log2() -> tuple[dict, ...]` in the legacy
  `{"QuoteTime", "bids", "asks"}` shape for the deprecated
  `OrderBook.book_log2` property and for code paths that need the
  numpy arrays.

- **`OrderBookHistoryMemorySink(symbol)`** — `accept_events = True`.
  Filters by `event_type in BOOK_EVENT_TYPES` and by payload
  `.symbol == symbol`.  Stores the payload `NamedTuple`s.  Exposes
  `as_history_dicts() -> tuple[dict, ...]` in the legacy
  `{"time", "type", **payload_fields}` shape (with `symbol` stripped,
  since the legacy history list never carried it).

One pair is registered per symbol; the compile path (and
`ExchangeAgent.kernel_initializing` as a backstop for the legacy
`build_config()` path) installs them on `kernel.event_bus`.

### 5.3 Book event vocabulary and payload schema

Six bare-string event types are published by `OrderBook`, defined in
`abides_markets.book_events`:

| `event_type` | `NamedTuple` payload | Fired at |
|---|---|---|
| `LIMIT` | `LimitPayload(symbol, order_id, agent_id, side, quantity, price)` | Every accepted limit order. |
| `EXEC` | `ExecPayload(symbol, order_id, agent_id, oppos_order_id, oppos_agent_id, side, quantity, price)` | Each fill (one per side per match). |
| `CANCEL` | `CancelPayload(symbol, order_id, tag, metadata)` | Full cancel. |
| `CANCEL_PARTIAL` | `CancelPartialPayload(symbol, order_id, quantity, tag, metadata)` | Partial cancel. |
| `MODIFY` | `ModifyPayload(symbol, order_id, new_side, new_quantity)` | In-place quantity / side modify. |
| `REPLACE` | `ReplacePayload(symbol, old_order_id, new_order_id, quantity, price)` | Cancel-and-replace. |

`symbol` is the **first** field of every payload so a single history
sink can demultiplex events from a multi-symbol exchange.  Bare strings
match the historical `history["type"]` literals — zero migration for
consumers that switch from reading `OrderBook.history` to walking the
sink.

The `BOOK_EVENT_PAYLOAD_CLASSES` dict in `abides_markets.book_events`
maps event types to their `NamedTuple` classes.

### 5.4 Producer side

`OrderBook` no longer owns capture state.  All snapshot writes flow
through `OrderBook._publish_snapshot(t)`:

```
mode = exchange.book_capture
if mode == "off": return
if mode == "l1":
    if (bid_top, ask_top) == cache: return     # publisher-side dedup
    cache = (bid_top, ask_top)
    bus.publish_book_snapshot(symbol, t, ((bid_p, bid_q),), ((ask_p, ask_q),), 1)
else:  # "l2"
    bus.publish_book_snapshot(symbol, t, l2_bids, l2_asks, stream_history)
```

All event writes flow through `OrderBook._publish_event(t, type_str,
payload)`:

```
bus.publish_event(exchange.id, "ExchangeAgent", t, type_str, payload)
```

Agent attribution lives **inside** the payload (`agent_id`,
`oppos_agent_id`); the producer field on the wire tuple is the
exchange.  When no kernel/bus is attached (standalone unit tests
constructing `OrderBook` against a stub agent), both methods append to
internal fallback buffers that the deprecated properties read instead.

### 5.5 Deprecated `OrderBook.book_log2` and `OrderBook.history`

Both attributes are now `@property` shims.  Each:

1. Walks `kernel.event_bus._sinks` to find the matching per-symbol
   `OrderBookSnapshotMemorySink` / `OrderBookHistoryMemorySink`.
2. Materializes the legacy list-of-dicts shape via `as_book_log2()` /
   `as_history_dicts()`.
3. Caches the result on the `OrderBook` instance; invalidates the
   cache when the sink length changes (so the cached list stays live
   across the whole simulation — `ExchangeAgent` re-reads
   `history[1:length+1]` on every `QueryOrderStreamMsg`).
4. Emits `DeprecationWarning` once per instance.

When no kernel/bus is attached, the properties fall back to the
in-process buffers populated by `_publish_snapshot` / `_publish_event`.

New code should read directly from the sinks via
`ExchangeAgent._get_snapshot_sink(symbol)` /
`ExchangeAgent._get_history_sink(symbol)`, or via
`SimulationResult.logs` once the relevant sink type is exposed there.

### 5.6 Reproducibility contract

With `book_capture="l2"` and a fixed seed, all
`SimulationResult.markets[symbol]` fields are byte-equivalent to a
pre-EventBus baseline pickled at
`abides-markets/tests/data/book_capture_baseline_l2.pkl` (asserted by
`test_book_capture_reproducibility::test_l2_byte_equivalent`).  With
`book_capture="l1"`, the L1 series equals the L2 series after
consecutive-duplicate removal (with the empty initial snapshot
dropped), and the L2 series is empty.

# Running Experiments in Parallel

*For researchers running Monte Carlo studies, parameter sweeps, and
stress-test batteries across many seeds or scenario variants.*

Covers `run_batch`, the multiprocessing model, the deterministic
per-worker RNG hierarchy, and the on-disk log layout each parallel
run produces.

> **Audience:** developers who need to launch multiple ABIDES
> simulations concurrently.

---

## 1. Architecture Overview

ABIDES is a **discrete-event simulation** framework. Each simulation is driven by a single `Kernel` instance that processes a `heapq`-managed priority list of timestamped messages, dispatching `wakeup()` and `receive_message()` calls to `Agent` objects sequentially.

**Key facts:**
- One `Kernel` = one simulation. The Kernel is single-threaded internally.
- Each `Kernel`, each `Agent`, the `LatencyModel`, and each Oracle hold their own `np.random.RandomState` — all derived deterministically from a master seed via identity-based hashing (`SHA-256`). Each component's seed depends only on the master seed and the component's name — adding or removing agent groups never shifts other components' seeds.
- There is **no built-in parallelism** within a simulation. For multi-run workloads, use Python's `multiprocessing` or `concurrent.futures` with separate `run_simulation()` calls per process.

### RNG Hierarchy (Identity-Based Derivation)

Each component derives its seed independently from the master seed via `hashlib.sha256(f"{seed}:{component}:{index}")`:

```
compile(config, seed=42)
├── oracle     → sha256("42:oracle:0")
├── exchange   → sha256("42:exchange:0")
├── agent group "noise"
│   ├── agent 0 → group_rng from sha256("42:agent:noise:0"), then sequential randint()
│   ├── agent 1 → ...
│   └── agent N → ...
├── agent group "value"
│   ├── agent 0 → group_rng from sha256("42:agent:value:0"), independent of noise
│   └── ...
├── kernel     → sha256("42:kernel:0")
└── latency    → sha256("42:latency:0")
```

**Composition invariance:** adding a new agent group (e.g. a custom strategy) does not change any existing agent's seed. This enables fair baseline-vs-strategy A/B comparison.

### Remaining Thread-Safety Concerns

The `Message` and `Order` ID generators use `itertools.count()`, which is GIL-safe in CPython. Both `MeanRevertingOracle` and `SparseMeanRevertingOracle` use injected `RandomState` objects (no global PRNG). However, in-process threading is still **not recommended** due to:

| # | Issue | Location | Severity |
|---|---|---|---|
| 1 | No simulation ID in log format — concurrent logs are interleaved | All modules | **LOW** |

**Use `multiprocessing` for parallel runs.** Each process gets its own memory space, eliminating all shared-state concerns.

---

## 2. Using `run_batch()` (Recommended)

The simplest way to run simulations in parallel is `run_batch()` from
`abides_markets.simulation`.  It accepts a list of `SimulationConfig` objects,
spawns worker processes, compiles each config independently, and returns a list
of immutable `SimulationResult` objects in input order.

```python
from abides_markets.config_system import SimulationBuilder
from abides_markets.simulation import run_batch

configs = [
    SimulationBuilder().apply_template("rmsc04").seed(s).build()
    for s in range(1, 9)
]

results = run_batch(configs)      # uses all available CPUs by default

for r in results:
    print(f"Seed {r.metadata.seed}: {r.markets['ABM'].l1_close}")
```

Each worker compiles its own runtime dict from the provided config — no shared
state, unique log directories (UUID-based by default), and fully deterministic
given the seed.  The returned `SimulationResult` objects are frozen Pydantic
models safe to share across threads.

For finer control over extraction, pass a `profile` or `extractors`:

```python
from abides_markets.simulation import run_batch, ResultProfile

results = run_batch(configs, profile=ResultProfile.QUANT, n_workers=4)
```

---

## 3. Manual Parallelism with `multiprocessing`

For cases where you need direct control over the worker processes (e.g.,
custom logging, non-standard return values), you can use `multiprocessing`
directly with the low-level `compile()` → `abides.run()` path.

### 3.1 Minimal Example

```python
import multiprocessing as mp
from abides_markets.config_system import SimulationBuilder
from abides_markets.simulation import run_simulation


def run_one_simulation(seed: int) -> dict:
    """
    Entry point for a single simulation in a worker process.
    Each process gets its own memory space — no shared state concerns.
    """
    config = (
        SimulationBuilder()
        .apply_template("rmsc04")
        .seed(seed)
        .build()
    )
    result = run_simulation(config)

    # Return only what you need — result is a frozen Pydantic model, picklable.
    return {
        "seed": seed,
        "l1_close": result.markets["ABM"].l1_close,
    }


def main():
    seeds = list(range(1, 9))

    with mp.Pool(processes=mp.cpu_count()) as pool:
        results = pool.map(run_one_simulation, seeds)

    for r in results:
        print(f"Seed {r['seed']}: l1_close={r['l1_close']}")


if __name__ == "__main__":
    main()
```

### 3.2 Using `concurrent.futures.ProcessPoolExecutor`

```python
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed

def main():
    seeds = list(range(1, 9))

    with ProcessPoolExecutor(max_workers=mp.cpu_count()) as executor:
        futures = {executor.submit(run_one_simulation, s): s for s in seeds}

        for future in as_completed(futures):
            seed = futures[future]
            result = future.result()
            print(f"Seed {seed}: done")
```

---

## 4. Critical Rules for Safe Parallel Runs

### 4.1 Always pass a unique `log_dir`

When using the low-level `abides.run()` path, the Kernel defaults `log_dir` to a UUID (`uuid.uuid4().hex`), which is collision-free. When writing to a shared results directory, pass an explicit `log_dir` to keep runs organised:

```python
run(config=config, log_dir=f"experiment_42/seed_{seed}")
```

With `run_simulation()` / `run_batch()`, the log directory is managed internally and does not need to be set by the caller.

### 4.2 Always pass an explicit seed

`SimulationBuilder.seed(n)` sets the master seed from which all component seeds are derived (see §5). With `run_simulation()`, the seed is part of the compiled config:

```python
config = SimulationBuilder().apply_template("rmsc04").seed(42).build()
result = run_simulation(config)
```

When using the low-level `abides.run()` path, omitting `kernel_seed` gives `kernel_seed=0` — deterministic, but if you run multiple low-level simulations without seeding them individually you will get identical outcomes. Always set an explicit seed.

### 4.3 Both oracles use injected `RandomState`

Both `MeanRevertingOracle` and `SparseMeanRevertingOracle` accept and use an injected `random_state` parameter — neither calls the global `np.random` PRNG. Results are fully deterministic given the seed. Both are safe for parallel runs.

### 4.4 Configure logging at the top level

The `abides_core.abides.run()` function does **not** configure logging. If you want colored log output, call `coloredlogs.install()` yourself at program startup (the CLI script `abides-core/scripts/abides` does this). In multiprocessing, each process has its own logger state, so log output from multiple processes will be interleaved on stdout unless you add per-process file handlers.

For clean per-simulation log files:

```python
import logging

def run_one_simulation(seed: int):
    # Set up a per-process file handler
    handler = logging.FileHandler(f"./log/sim_{seed}/simulation.log")
    handler.setFormatter(logging.Formatter(
        f"[sim_{seed}] %(levelname)s %(name)s %(message)s"
    ))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

    config = SimulationBuilder().apply_template("rmsc04").seed(seed).build()
    return run_simulation(config)
```

### 4.5 Return values must be picklable

`multiprocessing` serialises return values via `pickle`. `SimulationResult` is a frozen Pydantic model and is always picklable. If you use the low-level `abides.run()` path, the `end_state` dict contains `Agent` objects, DataFrames, and numpy arrays — picklable but large. Extract only what you need:

```python
def run_one_simulation(seed: int) -> dict:
    config = SimulationBuilder().apply_template("rmsc04").seed(seed).build()
    result = run_simulation(config)
    # SimulationResult is picklable — return it directly, or extract fields:
    return {"seed": seed, "l1_close": result.markets["ABM"].l1_close}
```

### 4.6 Gym environments

Each `AbidesGymCoreEnv.reset()` creates a fresh `Kernel` and full agent set. Gym environments are designed for sequential use (one env = one simulation at a time). To run multiple gym environments in parallel, use multiple processes — each with its own env instance. **Do not share gym env instances across threads.**

```python
# Each worker creates its own env — safe
def run_gym_episode(seed):
    env = SubGymEnv(...)  # your specific gym env subclass
    obs, info = env.reset(seed=seed)
    done = False
    while not done:
        action = your_policy(obs)
        obs, reward, terminated, truncated, info = env.step(action)
        done = terminated or truncated
    env.close()
    return info
```

---

## 5. RNG Hierarchy (How Seeds Flow)

Every component derives its seed independently from the master seed via SHA-256:

```
SimulationBuilder().seed(42).build()  →  compile(config)
│
├── oracle     → sha256("42:oracle:0")    → np.random.RandomState
├── exchange   → sha256("42:exchange:0")  → np.random.RandomState
├── kernel     → sha256("42:kernel:0")    → np.random.RandomState
├── latency    → sha256("42:latency:0")   → np.random.RandomState
└── agent groups (per registered name + index)
    ├── "noise" group
    │   ├── agent 0 → sha256("42:agent:noise:0")
    │   ├── agent 1 → sha256("42:agent:noise:1")
    │   └── ...
    └── "value" group  ← independent of "noise" group
        ├── agent 0 → sha256("42:agent:value:0")
        └── ...
```

**Every component gets its own `RandomState`, derived deterministically from the master seed. Adding or removing an agent group does not shift any other component's seed.** Given the same master seed, the same simulation produces identical results.

---

## 6. File Layout for Logs

See [logging-architecture.md](logging-architecture.md) for the current log-writer and sink pipeline. In brief: the recommended path is event-bus sinks (`ParquetSink`, `MemorySink`) configured per-simulation via `SimulationBuilder.add_sink(...)`. The legacy bz2 per-agent pickle layout (`ExchangeAgent0.bz2`, etc.) is deprecated and may be removed in a future release.

For parallel runs, each `run_simulation()` call manages its own sinks. If you configure a `ParquetSink` with a file path, ensure the path includes a seed or run-id component to avoid collisions across workers.

---

## 7. Common Pitfalls

| Pitfall | Consequence | Prevention |
|---|---|---|
| Not passing `seed` | Non-reproducible results (kernel_seed defaults to 0) | Always call `.seed(n)` on the builder |
| Sharing gym env across threads | Undefined behavior | One env per process |
| Returning full `end_state` from workers | Large pickle overhead | Extract only needed fields |
| Using `ThreadPoolExecutor` | Shared class-level counters across threads | Use `ProcessPoolExecutor` |

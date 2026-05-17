# Quickstart

*Run your first simulation in five minutes.*

This page walks through installing ABIDES-NG, running a one-day
scenario against a realistic limit-order book, and inspecting the
results. It assumes Python 3.12+ and a working `pip` (or `uv`).

---

## 1. Install

```bash
pip install abides-ng
```

For the reinforcement-learning stack (Gymnasium + RLlib):

```bash
pip install abides-ng[gym]
```

Verify the install:

```bash
python -c "from abides_markets.simulation import run_simulation; print('ok')"
```

---

## 2. Run a scenario

The `rmsc04` template ships with a calibrated mix of noise, value,
momentum, and market-making agents — a reasonable baseline for
strategy research and execution-quality measurement.

```python
from abides_markets.config_system import SimulationBuilder
from abides_markets.simulation import run_simulation

config = (
    SimulationBuilder()
    .from_template("rmsc04")
    .market(ticker="ABM")
    .seed(42)
    .build()
)

result = run_simulation(config)
print(result.summary())
```

`run_simulation` returns an immutable `SimulationResult` carrying the
final state of every trading agent, a market summary per symbol, and
(optionally) full L1/L2 book history and raw event logs. The same
`config` can be re-run any number of times — the results are
byte-identical for a fixed seed.

---

## 3. Inspect the results

```python
# Per-agent PnL (sorted)
for a in sorted(result.agents, key=lambda x: x.pnl_cents, reverse=True):
    print(f"[{a.agent_id:3d}] {a.agent_type:25s} "
          f"MtM=${a.mark_to_market_cents/100:>12,.2f} "
          f"PnL=${a.pnl_cents/100:>+12,.2f}")

# Per-symbol market summary
for symbol, mkt in result.markets.items():
    liq = mkt.liquidity
    print(f"{symbol}: vwap=${(liq.vwap_cents or 0)/100:.2f} "
          f"volume={liq.total_exchanged_volume:,}")
```

To get full event logs, L1/L2 book history, or per-trade attribution,
request a richer `ResultProfile` when running — see
[Extracting Results](../reference/data-extraction.md).

---

## 4. Add a custom strategy

Define a `TradingAgent` subclass, register it, and enable it in the
config:

```python
from abides_markets.agents import TradingAgent
from abides_markets.config_system import register_agent

class MyStrategy(TradingAgent):
    ...

register_agent("my_strategy", agent_class=MyStrategy, category="strategy")

config = (
    SimulationBuilder()
    .from_template("rmsc04")
    .enable_agent("my_strategy", count=1, my_param=42)
    .seed(42)
    .build()
)
```

See [Writing a Custom Agent](../reference/custom-agent-guide.md) for the
full adapter pattern, risk-config wiring, and a copy-paste scaffold.

---

## 5. Compute execution and microstructure metrics

```python
from abides_markets.simulation import ResultProfile
from abides_markets.simulation.metrics import compute_rich_metrics

# Re-run with the richest profile so the metrics have the data they need
result = run_simulation(config, result_profile=ResultProfile.FULL)
metrics = compute_rich_metrics(
    result,
    include_fills=True,
    adverse_selection_windows=("100ms", "1s"),
)

# Per-agent Sharpe, drawdown, VWAP slippage, fill rate, inventory std
for agent_id, m in metrics.agent_metrics.items():
    print(agent_id, m.sharpe_ratio, m.max_drawdown_cents, m.fill_rate_pct)

# Market-wide microstructure indicators
for symbol, ms in metrics.microstructure.items():
    print(symbol, ms.mean_spread_cents, ms.lob_imbalance_mean,
          ms.vpin, ms.market_ott_ratio)
```

The metrics catalogue (VWAP, implementation shortfall, LOB imbalance,
VPIN, MiFID II RTS 9 order-to-trade ratio, resilience, adverse
selection) is described in [Computing Metrics](../reference/metrics-api.md)
and [Metrics Algorithms](../reference/metrics-algorithms.md).

---

## Where to next

- [Building a Simulation](../reference/config-system.md) — full
  declarative config reference: templates, agent registry, risk
  controls, oracle modes, YAML serialization.
- [Writing a Custom Agent](../reference/custom-agent-guide.md) — the
  adapter pattern for plugging proprietary strategies into the
  simulator.
- [Extracting Results](../reference/data-extraction.md) — every
  data surface on `SimulationResult` and how to reconstruct L1 / L2
  book history.
- [Running Experiments in Parallel](../reference/parallel-simulation.md)
  — `run_batch`, deterministic per-worker seeding, and the log layout
  for Monte Carlo studies.
- [Reproducibility](../project/reproducibility.md) — what is guaranteed
  bit-for-bit across runs and what is not.

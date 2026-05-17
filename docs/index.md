# ABIDES-NG

**A research-grade agent-based market simulator for the financial industry.**

[![CI](https://github.com/GabrieleDiCorato/abides-ng/actions/workflows/ci.yml/badge.svg)](https://github.com/GabrieleDiCorato/abides-ng/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/abides-ng.svg)](https://pypi.org/project/abides-ng/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](https://github.com/GabrieleDiCorato/abides-ng/blob/main/LICENSE)

---

ABIDES-NG is a discrete-event simulator of a continuous limit-order
market. It lets quants, execution-quality analysts, risk teams, and
regulatory-research groups evaluate strategies and controls against a
**reactive book** — one where every order generates impact and every
agent responds to the orders of every other — instead of replaying a
historical tape as a price-taker.

It is an actively maintained, BSD-3-licensed continuation of the
[ABIDES](https://github.com/abides-sim/abides) research code from
Georgia Tech and J.P. Morgan AI Research, modernised and substantially
extended for production-grade simulation work. See
[Why ABIDES-NG](project/one-pager.md) for the full positioning.

```bash
pip install abides-ng
```

Then jump to the [Quickstart](getting-started/quickstart.md).

---

## What you can do with it

### Strategy research
Backtest alpha signals against a book that moves when you trade.
Quantify market impact and adverse selection rather than assuming them
away. Inject a new agent into an existing scenario without disturbing
the seeded behaviour of any other agent — a property no
sequential-seed simulator provides.

### Execution-quality measurement
Evaluate TWAP / VWAP / POV implementations end-to-end: arrival-price
slippage, implementation shortfall, participation rate, per-fill
VWAP comparison, and adverse selection at configurable horizons —
all produced as typed Pydantic models from a single
`compute_rich_metrics()` call.

### Risk & regulatory testing
Per-agent position limits, drawdown kill-switches, and order-rate
caps are declarative configuration, enforced at order entry. The
metrics pipeline computes the microstructure indicators that
regulators care about — including **MiFID II RTS 9 market-wide
order-to-trade ratios**, time-averaged quoted spread, LOB imbalance
(Cont, Kukanov & Stoikov 2014), VPIN (Easley et al. 2012), and
spread-resilience (Foucault et al. 2013). Stress-test books under
megashocks, thin-liquidity templates, and full-day volatile-regime
scenarios.

### Reinforcement-learning research
The `abides-ng[gym]` extra exposes the simulator as a Gymnasium
environment compatible with RLlib. Single- and multi-agent
configurations, deterministic per-worker seeding, and the full
RichSimulationMetrics surface as a reward / diagnostic source.

---

## What makes it different

| Capability | Typical academic ABM | ABIDES-NG |
|---|---|---|
| Configuration | Procedural scripts | Declarative `SimulationConfig` (YAML/JSON serialisable) |
| RNG hierarchy | Sequential seed draws — adding an agent shifts all other agents | SHA-256 identity hashing — order- and composition-invariant |
| Risk controls | Not present | Position limits, drawdown kill-switch, order-rate caps, all declarative |
| Order types | Limit + market | + IOC / FOK / DAY time-in-force, stop orders with exchange-side trigger |
| Analytics | Manual log parsing | `compute_rich_metrics()` → Sharpe, drawdown, VWAP, LOB imbalance, VPIN, OTT ratio, resilience, adverse selection |
| Parallel sweeps | Manual | `run_batch()` with multiprocessing and deterministic per-worker seeds |
| Reproducibility | Best-effort | Bit-for-bit; enforced by the regression suite |

---

## Where to go next

- **New to the project?** Start with the [Quickstart](getting-started/quickstart.md).
- **Building a scenario?** Read [Building a Simulation](reference/config-system.md).
- **Writing your own strategy?** Read [Writing a Custom Agent](reference/custom-agent-guide.md).
- **Running experiments at scale?** Read [Running Experiments in Parallel](reference/parallel-simulation.md).
- **Measuring execution / market microstructure?** Read [Computing Metrics](reference/metrics-api.md).
- **Auditing the architecture?** The
  [Architecture Reference](reference/kernel-architecture.md) section
  documents the kernel, event bus, logging pipeline, and event
  vocabulary.
- **Caring about reproducibility?** Read [Reproducibility](project/reproducibility.md).

---

## Source, license, contributing

Source: [github.com/GabrieleDiCorato/abides-ng](https://github.com/GabrieleDiCorato/abides-ng).
Licensed BSD-3-Clause (derivative of the original ABIDES code; see
[LICENSE](https://github.com/GabrieleDiCorato/abides-ng/blob/main/LICENSE)).
Contribution workflow is described in
[CONTRIBUTING.md](https://github.com/GabrieleDiCorato/abides-ng/blob/main/CONTRIBUTING.md).

# ABIDES-NG

**Agent-Based Interactive Discrete Event Simulation** for financial markets research.

[![CI](https://github.com/GabrieleDiCorato/abides-ng/actions/workflows/ci.yml/badge.svg)](https://github.com/GabrieleDiCorato/abides-ng/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](https://github.com/GabrieleDiCorato/abides-ng/blob/main/LICENSE)

---

ABIDES-NG is a research-grade discrete-event market simulator where **agents react to
events — there are no loops**. It provides a realistic order book, a rich analytics
pipeline, and a declarative configuration system for building reproducible experiments.

## Quick install

```bash
pip install abides-ng
```

For the RL stack (Gymnasium / RLlib):

```bash
pip install abides-ng[gym]
```

## Core concepts

| Concept | Description |
|---------|-------------|
| **Discrete-event kernel** | Agents are driven by `wakeup()` and `receive_message()`. No polling loops. |
| **Integer prices** | All prices are integer cents (`$100.00 = 10_000`). |
| **Declarative config** | `SimulationBuilder` → `run_simulation()` is the recommended entry point. |
| **EventBus** | Agent events, metrics, and order book snapshots flow through a typed bus with pluggable sinks. |
| **Reproducibility** | Fixed seed → byte-equivalent results, enforced by the test suite. |

## Navigating the docs

- **[Reference](reference/config-system.md)** — authoritative API and system documentation for every
  subsystem: config, kernel, logging, event bus, metrics.
- **[Project](project/roadmap.md)** — roadmap, release process, reproducibility policy, and
  architecture one-pager.
- **[Changelog](changelog.md)** — full version history.

## Source & contributing

The full source is on [GitHub](https://github.com/GabrieleDiCorato/abides-ng). See
[CONTRIBUTING.md](https://github.com/GabrieleDiCorato/abides-ng/blob/main/CONTRIBUTING.md)
for the development workflow.

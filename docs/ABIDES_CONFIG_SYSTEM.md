# ABIDES — Declarative Configuration System

This document covers the pluggable, AI-friendly configuration system for ABIDES
market simulations. It replaces (or complements) the procedural `build_config()`
functions with declarative Pydantic models, YAML/JSON serialization, and
composable templates.

---

## Quick Start

```python
from abides_markets.config_system import SimulationBuilder, compile
from abides_core import abides

# Build a config from a template
config = (SimulationBuilder()
    .from_template("rmsc04")
    .market(ticker="AAPL")
    .seed(42)
    .build())

# Compile to Kernel runtime dict
runtime = compile(config)

# Run the simulation
end_state = abides.run(runtime)
```

---

## Architecture

The config system has four layers:

| Layer | Module | Purpose |
|-------|--------|---------|
| **Models** | `models.py` | Pydantic models for `SimulationConfig` and its sections |
| **Registry** | `registry.py` | Agent type registration with `@register_agent` decorator |
| **Builder** | `builder.py` | Fluent API: `SimulationBuilder().from_template(...).build()` |
| **Compiler** | `compiler.py` | Converts `SimulationConfig` → Kernel runtime dict |

Supporting modules: `templates.py` (composable presets), `serialization.py`
(YAML/JSON I/O), `agent_configs.py` (per-agent Pydantic configs with factories),
`builtin_registrations.py` (auto-registers 5 built-in agents).

---

## Configuration Structure

`SimulationConfig` has four clearly separated sections:

```yaml
market:
  ticker: ABM
  date: "20210205"
  start_time: "09:30:00"
  end_time: "10:00:00"
  oracle:
    type: sparse_mean_reverting
    r_bar: 100000          # $1000.00 in cents
    kappa: 1.67e-16
    fund_vol: 5.0e-05
  exchange:
    book_logging: true
    book_log_depth: 10
    computation_delay: 0

agents:
  noise:
    enabled: true
    count: 1000
    params: {}
  value:
    enabled: true
    count: 102
    params:
      r_bar: 100000
      kappa: 1.67e-15
      lambda_a: 5.7e-12
      computation_delay: 200   # per-agent-type override (ns)
  momentum:
    enabled: true
    count: 12
    params:
      wake_up_freq: "37s"

infrastructure:
  latency:
    type: deterministic
  default_computation_delay: 50   # global default (ns)

simulation:
  seed: 42
  log_level: INFO
  log_orders: true
```

---

## Templates

Templates are composable presets. Base templates provide a full config;
overlay templates add agent groups without replacing existing ones.

| Template | Type | Description |
|----------|------|-------------|
| `rmsc04` | base | Reference config: 1000 Noise, 102 Value, 12 Momentum, 2 MM |
| `liquid_market` | base | High liquidity: 5000 Noise, 200 Value, 25 Momentum, 4 MM |
| `thin_market` | base | Low liquidity: 100 Noise, 20 Value, no MM |
| `with_momentum` | overlay | Adds 12 Momentum agents |
| `with_execution` | overlay | Adds 1 POV Execution agent |

Stack templates: later ones override earlier ones.

```python
config = (SimulationBuilder()
    .from_template("rmsc04")
    .from_template("with_execution")   # adds execution agent
    .seed(42)
    .build())
```

---

## Builder API

The `SimulationBuilder` provides a fluent interface:

```python
config = (SimulationBuilder()
    .from_template("rmsc04")
    .market(ticker="AAPL", date="20220315")
    .oracle(r_bar=150_000)
    .exchange(book_log_depth=20)
    .enable_agent("noise", count=500)
    .enable_agent("value", count=50, r_bar=200_000, computation_delay=100)
    .disable_agent("momentum")
    .agent_computation_delay("noise", 200)  # set per-type delay
    .latency(type="deterministic")
    .computation_delay(75)                  # global default
    .seed(42)
    .log_level("DEBUG")
    .log_orders(True)
    .build())
```

---

## Per-Agent Computation Delays

Every agent group can specify a `computation_delay` (in nanoseconds) that
overrides the simulation-level `default_computation_delay`. This controls
how long an agent "thinks" after each wakeup or message receipt.

**Use cases:**
- Market makers with fast computation (low delay)
- Background noise agents with high delay
- Execution agents with realistic processing times

```python
# Via builder
config = (SimulationBuilder()
    .from_template("rmsc04")
    .enable_agent("adaptive_market_maker", count=2, computation_delay=10)
    .enable_agent("noise", count=1000, computation_delay=500)
    .computation_delay(50)   # default for agents without override
    .seed(42)
    .build())

# Via YAML
# agents:
#   adaptive_market_maker:
#     enabled: true
#     count: 2
#     params:
#       computation_delay: 10
```

The compiler produces a `per_agent_computation_delays` dict in the runtime
config, which the Kernel applies on initialization.

---

## Agent Registry

Agent types self-register via the `@register_agent` decorator or
`registry.register()`. Built-in agents are registered at import time.

### Registered built-in agents

| Name | Category | Config class |
|------|----------|-------------|
| `noise` | background | `NoiseAgentConfig` |
| `value` | background | `ValueAgentConfig` |
| `momentum` | strategy | `MomentumAgentConfig` |
| `adaptive_market_maker` | market_maker | `AdaptiveMarketMakerConfig` |
| `pov_execution` | execution | `POVExecutionAgentConfig` |

### Registering a custom agent

```python
from pydantic import Field
from abides_markets.config_system import BaseAgentConfig, register_agent

@register_agent("my_strategy", category="strategy", description="My custom strategy")
class MyStrategyConfig(BaseAgentConfig):
    threshold: float = Field(default=0.05)
    computation_delay: int = Field(default=100)  # optional per-agent delay

    def create_agents(self, count, id_start, master_rng, context):
        # Return a list of agent instances
        ...
```

---

## Serialization (YAML / JSON)

```python
from abides_markets.config_system import save_config, load_config

# Save
save_config(config, "my_sim.yaml")
save_config(config, "my_sim.json")

# Load
config = load_config("my_sim.yaml")
```

---

## AI Discoverability API

```python
from abides_markets.config_system import (
    list_agent_types,
    list_templates,
    get_config_schema,
    validate_config,
)

# What agent types are available?
list_agent_types()
# → [{"name": "noise", "category": "background", "parameters": {...}}, ...]

# What templates are available?
list_templates()
# → [{"name": "rmsc04", "description": "...", "agent_types": [...]}, ...]

# Full JSON Schema
get_config_schema()

# Validate a config dict
validate_config({"simulation": {"seed": 42}})
# → {"valid": True}
```

---

## Backward Compatibility

The old `build_config()` functions (e.g., `rmsc04.build_config(seed=42)`)
continue to work unchanged. The new system produces the same runtime dict
format, so `abides.run()`, gymnasium environments, and `config_add_agents()`
all work with either approach.

# ABIDES-Gym

**Gymnasium and Ray/RLlib integration layer for ABIDES market simulations.**

ABIDES-Gym wraps the ABIDES-Markets discrete-event simulator into
standard [Gymnasium](https://gymnasium.farama.org/) environments, enabling
reinforcement-learning agents to interact with a realistic, agent-populated
order book. Two ready-to-use environments are provided — a daily investment
problem and an algorithmic execution problem — and the base classes are
designed to make it straightforward to build new financial RL environments
on top of the same infrastructure.

## Registered environments

| Environment ID | Class | Problem |
|---------------|-------|---------|
| `markets-daily_investor-v0` | `SubGymMarketsDailyInvestorEnv_v0` | Maximize end-of-day marked-to-market portfolio value by trading a single stock. |
| `markets-execution-v0` | `SubGymMarketsExecutionEnv_v0` | Execute a parent order (e.g., buy 1 000 shares) over a fixed time window while minimizing slippage and market impact. |

Both environments are registered with Gymnasium and Ray/RLlib at import
time — `import abides_gym` is all that is needed.

## Quick start

### Gymnasium

```python
import gymnasium as gym
import abides_gym  # registers environments

env = gym.make(
    "markets-daily_investor-v0",
    background_config="rmsc04",
)

obs, info = env.reset(seed=42)
for _ in range(100):
    action = env.action_space.sample()
    obs, reward, terminated, truncated, info = env.step(action)
    if terminated or truncated:
        obs, info = env.reset()
```

### Ray/RLlib

```python
from ray import tune

tuner = tune.Tuner(
    "DQN",
    param_space={
        "env": "markets-execution-v0",
        "env_config": {
            "background_config": "rmsc04",
            "timestep_duration": "30S",
            "parent_order_size": 1000,
            "order_fixed_size": 100,
        },
    },
)
results = tuner.fit()
```

## Environment details

### `markets-daily_investor-v0`

A day-trading MDP. The agent starts with cash and no position, and
repeatedly buys or sells a fixed number of shares to maximize its
marked-to-market value at market close.

**Action space** — `Discrete(3)`: `0` = BUY, `1` = HOLD, `2` = SELL.

**Observation space** — `Box` with `4 + state_history_length - 1` features:

| Index | Feature | Range |
|-------|---------|-------|
| 0 | Current holdings (shares) | unbounded |
| 1 | Order-book imbalance (depth 3) | [0, 1] |
| 2 | Bid–ask spread | unbounded |
| 3 | Direction feature (mid − last trade) | unbounded |
| 4 … | Padded historical returns | unbounded |

**Reward** — Dense mode: per-step change in marked-to-market value,
normalized by order size and number of steps. Sparse mode: reward only at
episode end.

**Termination** — Portfolio value falls below `done_ratio × starting_cash`.
**Truncation** — Market close is reached.

**Key parameters:** `background_config`, `timestep_duration`,
`starting_cash`, `order_fixed_size`, `state_history_length`,
`reward_mode` (`"dense"` | `"sparse"`), `done_ratio`.

### `markets-execution-v0`

An optimal-execution MDP. The agent receives a parent order (size and
direction) and a time window, and must split it into child orders while
balancing urgency against market impact.

**Action space** — `Discrete(3)`: `0` = market order, `1` = limit order at
near-touch, `2` = hold.

**Observation space** — `Box` with `8 + state_history_length - 1` features:

| Index | Feature | Range |
|-------|---------|-------|
| 0 | Holdings / parent order size | [−2, 2] |
| 1 | Time elapsed / execution window | [−2, 2] |
| 2 | Holdings % − time % | [−4, 4] |
| 3 | Book imbalance (all levels) | [0, 1] |
| 4 | Book imbalance (top 5 levels) | [0, 1] |
| 5 | Log price impact vs. entry | unbounded |
| 6 | Bid–ask spread | unbounded |
| 7 | Direction feature (mid − last trade) | unbounded |
| 8 … | Padded historical returns | unbounded |

**Reward** — Per-step slippage PnL (fill price vs. entry price), plus an
end-of-episode penalty for under- or over-execution.

**Termination** — Parent order fully executed.
**Truncation** — Execution window expires.

**Key parameters:** `background_config`, `timestep_duration`,
`parent_order_size`, `order_fixed_size`, `execution_window`, `direction`
(`"BUY"` | `"SELL"`), `not_enough_reward_update`, `too_much_reward_update`.

## Class hierarchy

```
gymnasium.Env
└── AbidesGymCoreEnv          # Kernel lifecycle, step/reset loop
    └── AbidesGymMarketsEnv   # Market-specific defaults, agent wiring
        ├── SubGymMarketsDailyInvestorEnv_v0
        └── SubGymMarketsExecutionEnv_v0
```

New environments subclass `AbidesGymMarketsEnv` and implement five
abstract methods that define the MDP:

```python
raw_state_to_state(raw_state)          # observation
raw_state_to_reward(raw_state)         # step reward
raw_state_to_done(raw_state)           # termination flag
raw_state_to_update_reward(raw_state)  # terminal reward adjustment
raw_state_to_info(raw_state)           # info dict
```

## Module layout

```
abides-gym/
├── abides_gym/
│   ├── __init__.py                    # Gymnasium & Ray registration
│   ├── envs/
│   │   ├── core_environment.py        # AbidesGymCoreEnv (abstract)
│   │   ├── markets_environment.py     # AbidesGymMarketsEnv (abstract)
│   │   ├── markets_daily_investor_environment_v0.py
│   │   ├── markets_execution_environment_v0.py
│   │   └── markets_execution_custom_metrics.py  # RLlib callbacks
│   └── experimental_agents/
│       ├── core_gym_agent.py          # Abstract RL agent interface
│       └── financial_gym_agent.py     # Market-aware RL agent bridge
└── scripts/
    ├── gym_runner.py                  # Execution env example
    ├── gym_runner_daily_investor.py   # Daily investor env example
    ├── rllib_runner.py                # RLlib DQN training example
    └── simple_rllib_runner.py         # Minimal RLlib example
```

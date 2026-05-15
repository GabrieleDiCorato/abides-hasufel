"""Benchmark: one ABIDES-Gym episode end-to-end with logging off.

What this measures
------------------
Wall-clock time of one full episode of
``SubGymMarketsDailyInvestorEnv_v0`` (which wraps an ``rmsc04``
background sim). The gym layer already sets ``book_logging=False``,
``exchange_log_orders=False``, and the kernel defaults to
``skip_log=True`` — so this is the pure compute path through the
gym → kernel → agent boundary.

Why this matters
----------------
The refactor must not regress the gym training loop. Gym episodes are
the dominant consumer of ABIDES in research workflows; even modest
per-step overhead compounds across millions of training steps.

Output
------
Appends one JSON line to
``benchmarks/results/gym_episode_throughput_no_sinks.jsonl``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import record_result, time_iterations  # noqa: E402

from abides_gym.envs.markets_daily_investor_environment_v0 import (  # noqa: E402
    SubGymMarketsDailyInvestorEnv_v0,
)

SCRIPT_NAME = "gym_episode_throughput_no_sinks"

# 5-minute simulated horizon, action every 60 s → ~5 steps/episode.
MKT_CLOSE = "09:35:00"
TIMESTEP_DURATION = "60s"
FIRST_INTERVAL = "00:00:30"
SEED = 12345

N_WARMUP = 1
N_ITER = 5

# Hold action — keeps the episode trajectory deterministic and exercises
# the full gym→kernel→agents loop without skewing toward order-heavy paths.
HOLD_ACTION = 1


def _run_episode() -> None:
    env = SubGymMarketsDailyInvestorEnv_v0(
        background_config="rmsc04",
        mkt_close=MKT_CLOSE,
        timestep_duration=TIMESTEP_DURATION,
        first_interval=FIRST_INTERVAL,
        debug_mode=False,
    )
    env.reset(seed=SEED)
    while True:
        _state, _reward, terminated, truncated, _info = env.step(HOLD_ACTION)
        if terminated or truncated:
            break


def measure() -> dict:
    stats = time_iterations(_run_episode, n_warmup=N_WARMUP, n_iter=N_ITER)
    return {
        "background_config": "rmsc04",
        "mkt_close": MKT_CLOSE,
        "timestep_duration": TIMESTEP_DURATION,
        "first_interval": FIRST_INTERVAL,
        "seed": SEED,
        "action_policy": "hold",
        **stats,
    }


if __name__ == "__main__":
    payload = measure()
    out_path = record_result(SCRIPT_NAME, payload)
    print(f"Recorded → {out_path}")
    print(
        f"  mkt_close={MKT_CLOSE}  "
        f"mean={payload['mean_ns'] / 1e9:.2f} s  "
        f"p99={payload['p99_ns'] / 1e9:.2f} s"
    )

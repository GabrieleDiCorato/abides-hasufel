"""Benchmark: rmsc04 simulation throughput with default logging sinks ON.

What this measures
------------------
End-to-end wall-clock time of one ``rmsc04`` simulation with the
out-of-the-box logging configuration: all agents log orders, the
exchange snapshots its book, and the kernel writes a summary log.

Why this matters
----------------
The refactor must not regress the default-sinks path. Phase 1 / Phase 2
gates state "no regression with default sinks". This script pins the
baseline so reviewers can verify.

Note: ``skip_log=True`` is *still* set to avoid actually writing pickle
files to disk on every iteration (which would dominate the measurement
and pollute the working tree). The agent-side and exchange-side log
buffers still build up in memory — that is what we are measuring.

Output
------
Appends one JSON line to
``benchmarks/results/single_agent_run_with_default_sinks.jsonl``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import record_result, time_iterations  # noqa: E402

from abides_core import abides  # noqa: E402
from abides_markets.config_system import SimulationBuilder  # noqa: E402

SCRIPT_NAME = "single_agent_run_with_default_sinks"

END_TIME = "09:35:00"
SEED = 12345
N_WARMUP = 1
N_ITER = 5


def _run_once() -> None:
    # rmsc04 template defaults: log_orders=True, book_capture="l2"
    runtime = (
        SimulationBuilder()
        .apply_template("rmsc04")
        .seed(SEED)
        .end_time(END_TIME)
        .log_level("WARNING")
        .build_and_compile()
    )
    # Suppress disk writes only — in-memory log buffers still accumulate.
    runtime["skip_log"] = True
    abides.run(runtime)


def measure() -> dict:
    stats = time_iterations(_run_once, n_warmup=N_WARMUP, n_iter=N_ITER)
    return {
        "end_time": END_TIME,
        "seed": SEED,
        "log_orders": True,
        "book_capture": "l2",
        "skip_log": True,
        **stats,
    }


if __name__ == "__main__":
    payload = measure()
    out_path = record_result(SCRIPT_NAME, payload)
    print(f"Recorded → {out_path}")
    print(
        f"  end_time={END_TIME}  "
        f"mean={payload['mean_ns'] / 1e9:.2f} s  "
        f"max={payload['max_ns'] / 1e9:.2f} s"
    )

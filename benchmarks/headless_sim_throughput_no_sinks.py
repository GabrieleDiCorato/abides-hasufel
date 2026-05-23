"""Benchmark: rmsc04 simulation throughput with all logging sinks off.

What this measures
------------------
End-to-end wall-clock time of one ``rmsc04`` simulation with:

* ``skip_log=True`` (Kernel-level: no per-agent log files, no summary log)
* ``log_orders=False`` (agents do not call ``logEvent`` on every order)
* ``book_logging=False`` (exchange does not snapshot the L2 book)
* ``exchange_log_orders=False`` (exchange does not log every order msg)

This is the "pure compute" baseline — it isolates the simulation engine
from the event-recording machinery the refactor is targeting.

Why this matters
----------------
Phase 2 of the event-logging refactor claims a ≥ 1.5× throughput
improvement on this configuration. This script pins the baseline.

Output
------
Appends one JSON line to ``benchmarks/results/headless_sim_throughput_no_sinks.jsonl``.

Tuning
------
``END_TIME`` is set to ``"09:35:00"`` → 5 minutes of simulated time.
That keeps each iteration to ~10–30 s on a typical laptop.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import record_result, time_iterations  # noqa: E402

from abides_core import abides  # noqa: E402
from abides_markets.config_system import ExchangeConfig, SimulationBuilder  # noqa: E402

SCRIPT_NAME = "headless_sim_throughput_no_sinks"

END_TIME = "09:35:00"  # 5-minute simulated horizon
SEED = 12345
N_WARMUP = 1
N_ITER = 5


def _run_once() -> None:
    runtime = (
        SimulationBuilder()
        .apply_template("rmsc04")
        .seed(SEED)
        .end_time(END_TIME)
        .log_level("WARNING")
        .log_orders(False)
        .exchange(ExchangeConfig(book_capture="off"))
        .build_and_compile()
    )
    runtime["skip_log"] = True
    abides.run(runtime)


def measure() -> dict:
    stats = time_iterations(_run_once, n_warmup=N_WARMUP, n_iter=N_ITER)
    return {
        "end_time": END_TIME,
        "seed": SEED,
        "log_orders": False,
        "book_capture": "off",
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

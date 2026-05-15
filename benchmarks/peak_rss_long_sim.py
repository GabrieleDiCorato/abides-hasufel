"""Benchmark: peak resident-set size during a longer-horizon rmsc04 sim.

What this measures
------------------
Peak Python (``tracemalloc``) heap and peak OS resident-set size during
a single ``rmsc04`` simulation with **default sinks on** but disk
writes suppressed (so the in-memory log buffers are what we measure).

The horizon is set to ``"10:00:00"`` (30 minutes of simulated time) —
long enough to surface unbounded buffer growth that a 5-minute sim
would mask, while still completing in a reasonable wall-clock time.

Why this matters
----------------
Phase 3 of the event-logging refactor targets memory-bounded log
buffers (ring buffers, columnar storage). This script pins the
unbounded baseline so reviewers can verify the eventual reduction.

Output
------
Appends one JSON line to ``benchmarks/results/peak_rss_long_sim.jsonl``.
The line includes ``peak_traced_python_bytes`` (Python objects only,
isolated to the call) and ``peak_rss_bytes`` (process-wide, includes
C-extension allocations from numpy/pandas).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import measure_peak_rss_during, record_result  # noqa: E402

from abides_core import abides  # noqa: E402
from abides_markets.configs.rmsc04 import build_config  # noqa: E402

SCRIPT_NAME = "peak_rss_long_sim"

END_TIME = "10:00:00"  # 30-minute simulated horizon
SEED = 12345


def _run_once() -> None:
    config = build_config(
        seed=SEED,
        end_time=END_TIME,
        # Default sinks ON — this is the baseline to beat.
        log_orders=True,
        book_logging=True,
        stdout_log_level="WARNING",
    )
    config["skip_log"] = True  # Suppress disk writes; in-memory buffers stand.
    abides.run(config)


def measure() -> dict:
    rss = measure_peak_rss_during(_run_once)
    return {
        "end_time": END_TIME,
        "seed": SEED,
        "log_orders": True,
        "book_logging": True,
        "skip_log_disk": True,
        **rss,
    }


if __name__ == "__main__":
    payload = measure()
    out_path = record_result(SCRIPT_NAME, payload)
    print(f"Recorded → {out_path}")
    print(
        f"  end_time={END_TIME}  "
        f"elapsed={payload['elapsed_ns'] / 1e9:.2f} s  "
        f"peak_python={payload['peak_traced_python_bytes'] / 1e6:.1f} MB  "
        f"peak_rss={payload['peak_rss_bytes'] / 1e6:.1f} MB"
    )

"""Benchmark: ``parse_logs_df`` latency on synthetic agent logs.

What this measures
------------------
Wall-clock latency of :func:`abides_core.utils.parse_logs_df` over a
fixed-size synthetic workload (``N_AGENTS`` agents, each holding
``ROWS_PER_AGENT`` log entries — totalling ``N_ROWS`` rows).

Why this matters
----------------
:func:`parse_logs_df` is the dominant log-parsing primitive used by
``runner._extract_*`` and notebook-side analysis. Phase 1 of the
event-logging refactor (commit ``aa7231e``) replaced a per-agent
``pd.concat`` loop with a single ``DataFrame.from_records`` over a
flat row list. The plan claims a ≥ 1.3× wall-clock speedup. This
script lets you re-run on ``aa7231e^`` and ``aa7231e`` to verify.

Output
------
Appends one JSON line to ``benchmarks/results/parse_logs_df_p99.jsonl``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script: ``python benchmarks/parse_logs_df_p99.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import record_result, time_iterations  # noqa: E402

from abides_core.utils import parse_logs_df  # noqa: E402

SCRIPT_NAME = "parse_logs_df_p99"

N_AGENTS = 100
ROWS_PER_AGENT = 10_000  # → 1_000_000 total rows
N_WARMUP = 2
N_ITER = 10


class _FakeAgent:
    __slots__ = ("id", "type", "log")

    def __init__(self, agent_id: int, log: list) -> None:
        self.id = agent_id
        self.type = "FakeAgent"
        self.log = log


def _build_agents() -> list[_FakeAgent]:
    agents: list[_FakeAgent] = []
    for agent_id in range(N_AGENTS):
        log: list[tuple[int, str, object]] = []
        for i in range(ROWS_PER_AGENT):
            # Mix of event shapes to exercise the three branches in
            # parse_logs_df: dict, scalar, None.
            if i % 3 == 0:
                event: object = {"price": 10_000 + i, "size": 100, "side": "BID"}
            elif i % 3 == 1:
                event = i  # scalar
            else:
                event = None
            log.append((1_700_000_000_000_000_000 + i * 1000, "ORDER_EVENT", event))
        agents.append(_FakeAgent(agent_id, log))
    return agents


def measure() -> dict:
    agents = _build_agents()
    total_rows = N_AGENTS * ROWS_PER_AGENT
    stats = time_iterations(
        lambda: parse_logs_df(agents),
        n_warmup=N_WARMUP,
        n_iter=N_ITER,
    )
    return {
        "n_agents": N_AGENTS,
        "rows_per_agent": ROWS_PER_AGENT,
        "total_rows": total_rows,
        **stats,
    }


if __name__ == "__main__":
    payload = measure()
    out_path = record_result(SCRIPT_NAME, payload)
    print(f"Recorded → {out_path}")
    print(
        f"  total_rows={payload['total_rows']:,}  "
        f"mean={payload['mean_ns'] / 1e6:.1f} ms  "
        f"p99={payload['p99_ns'] / 1e6:.1f} ms"
    )

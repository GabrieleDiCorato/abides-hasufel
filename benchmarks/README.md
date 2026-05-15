# ABIDES one-shot benchmark scripts

These scripts produce baseline measurements for the perf claims in the
[event-logging refactor plan](../docs/project/event-logging-refactor-plan.md).

**They are not CI gates.** No GitHub Actions job runs them. They are not
wired into `pytest`. They exist so that whoever is implementing a
performance-sensitive phase of the refactor can:

1. Check out the commit *before* their change, run the relevant script,
   capture the new line in `results/<script>.jsonl`.
2. Apply the change, run the same script, capture the second line.
3. Paste the delta into the PR description.

Hardware variance is real. Numbers from script runs are only meaningful
when compared **on the same machine, in similar load conditions**. Do not
compare numbers across machines — compare deltas only.

These scripts may be removed in two minor releases if no commit touches
them. They are scaffolding, not infrastructure.

## Scripts

| Script | Measures |
|---|---|
| `parse_logs_df_p99.py` | `parse_logs_df` latency on 1M synthetic rows. Pins the §3 / Phase 1 columnar gates. |
| `headless_sim_throughput_no_sinks.py` | rmsc04 sim with logging fully disabled. Pins the Phase 2 ≥ 1.5× gate. |
| `single_agent_run_with_default_sinks.py` | rmsc04 sim with default disk-write sinks on. Pins the "no regression with default sinks" gate. |
| `gym_episode_throughput_no_sinks.py` | One full `markets_daily_investor_v0` episode with logging off. Pins the gym-side no-regression gate. |
| `peak_rss_long_sim.py` | Peak RSS during a longer-horizon rmsc04 sim. Documents the unbounded-growth baseline so Phase 3 has a target. |

## Running

```bash
uv run python benchmarks/parse_logs_df_p99.py
uv run python benchmarks/headless_sim_throughput_no_sinks.py
# ...etc
```

Each script appends one JSON line to `benchmarks/results/<script>.jsonl`
of the form:

```json
{
  "commit": "abc1234",
  "timestamp_iso": "2026-05-15T12:34:56+00:00",
  "hostname": "...",
  "platform": "Windows-10-AMD64",
  "python": "3.12.7",
  "n_iter": 10,
  "n_warmup": 2,
  "mean_ns": 12345678,
  "p50_ns": 12000000,
  "p99_ns": 15000000,
  "min_ns": 11000000,
  "max_ns": 16000000
}
```

## Tuning

Each script sets `N_WARMUP` and `N_ITER` constants near the top.
Increase them for tighter numbers at the cost of wall-clock time.
Defaults are tuned to complete each script in well under five minutes
on a typical developer laptop.

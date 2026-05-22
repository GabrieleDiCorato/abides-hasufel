"""
ABIDES Risk-Manager Evaluation Script
======================================
Runs a 2-hour RMSC04 session and audits the resulting market against:
  1. Structural sanity (warnings, one-sided market time, zero-trade checks)
  2. Price discovery (VWAP vs fundamental, bid-ask spread, mean reversion)
  3. PnL attribution (conservation of wealth, agent-type breakdown)
  4. Microstructure stylised facts (return autocorrelation, Amihud illiquidity)
"""

from __future__ import annotations

import sys
import warnings
from collections import defaultdict

import numpy as np

from abides_markets.config_system import SimulationBuilder
from abides_markets.simulation import ResultProfile, run_simulation

SEED = 42
TICKER = "ABM"
R_BAR = 100_000          # fundamental mean in cents  ($1 000.00)
STARTING_CASH = 10_000_000   # cents

# ─────────────────────────────────────────────────────────────
# 1.  Run simulation
# ─────────────────────────────────────────────────────────────
print("=" * 70)
print("ABIDES — Risk-Manager Evaluation")
print(f"  Seed: {SEED}  |  Ticker: {TICKER}  |  Session: 09:30 – 11:30")
print("=" * 70)

print("\n[1/5] Running simulation …")
with warnings.catch_warnings(record=True) as captured_warnings:
    warnings.simplefilter("always")
    config = (
        SimulationBuilder()
        .from_template("rmsc04")
        .seed(SEED)
        .market(end_time="11:30:00")
        .build()
    )
    result = run_simulation(config, profile=ResultProfile.QUANT)

print(f"      Wall-clock elapsed: {result.metadata.wall_clock_elapsed_s:.1f}s")
print(f"      Python warnings captured: {len(captured_warnings)}")
for w in captured_warnings:
    print(f"        [WARN/{w.category.__name__}] {w.message}")

# ─────────────────────────────────────────────────────────────
# 2.  Structural sanity — summary_dict warnings
# ─────────────────────────────────────────────────────────────
print("\n[2/5] Structural sanity checks …")
sd = result.summary_dict()

sim_warnings = sd.get("warnings", [])
if sim_warnings:
    print(f"      RESULT WARNINGS ({len(sim_warnings)}):")
    for w in sim_warnings:
        print(f"        ⚠  {w}")
else:
    print("      OK  No structural warnings in SimulationResult")

mkt = result.markets[TICKER]
liq = mkt.liquidity
print(f"\n  Liquidity metrics for {TICKER}:")
print(f"    % time with no bid  : {liq.pct_time_no_bid:6.2f}%")
print(f"    % time with no ask  : {liq.pct_time_no_ask:6.2f}%")
print(f"    Total volume (shares): {liq.total_exchanged_volume:,}")
print(f"    Last trade price    : ${liq.last_trade_cents / 100:,.2f}" if liq.last_trade_cents else "    Last trade price    : N/A")
print(f"    VWAP                : ${liq.vwap_cents / 100:,.2f}" if liq.vwap_cents else "    VWAP                : N/A")

if liq.total_exchanged_volume == 0:
    print("\n  ❌  CRITICAL: zero trades executed — market did not function")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────
# 3.  Price discovery
# ─────────────────────────────────────────────────────────────
print("\n[3/5] Price discovery …")
l1 = mkt.l1_series
assert l1 is not None, "L1 series unexpectedly None under QUANT profile"

# Reconstruct mid-prices (filter ticks where both sides exist)
bids = np.array([x for x in l1.bid_prices], dtype=object)
asks = np.array([x for x in l1.ask_prices], dtype=object)
valid = np.array([(b is not None and a is not None) for b, a in zip(bids, asks)])
mid = np.array([(b + a) / 2 for b, a in zip(bids[valid], asks[valid])], dtype=float)
spread_cents = np.array([a - b for b, a in zip(bids[valid], asks[valid])], dtype=float)
times_ns = l1.times_ns[valid]

n_events = len(l1.times_ns)
n_two_sided = int(valid.sum())
pct_two_sided = 100 * n_two_sided / n_events if n_events else 0

print(f"  Book events total   : {n_events:,}")
print(f"  Two-sided events    : {n_two_sided:,}  ({pct_two_sided:.1f}%)")

mean_mid = float(np.mean(mid))
mean_spread = float(np.mean(spread_cents))
spread_bps = 10_000 * mean_spread / mean_mid  # basis points

print(f"\n  Mean mid-price      : ${mean_mid / 100:,.2f}")
print(f"  Fundamental r_bar   : ${R_BAR / 100:,.2f}")
drift_pct = 100 * (mean_mid - R_BAR) / R_BAR
print(f"  Mid vs fundamental  : {drift_pct:+.2f}%")
print(f"  Mean bid-ask spread : {mean_spread:.1f} cents  ({spread_bps:.2f} bps)")

# Typical liquid equity spreads: 1-5 bps; illiquid: >20 bps
if spread_bps < 0.5:
    print("  ⚠  Spread extremely tight (possible crossing / stale data)")
elif spread_bps < 50:
    print("  ✓  Spread in plausible range for simulated market")
else:
    print("  ⚠  Spread very wide — liquidity may be impaired")

# Price mean-reversion: run OLS of Δmid on mid-R_bar
if len(mid) > 50:
    gap = mid[:-1] - R_BAR
    delta_mid = np.diff(mid)
    cov = np.cov(gap, delta_mid)
    kappa_est = -cov[0, 1] / np.var(gap) if np.var(gap) > 0 else float("nan")
    print(f"\n  Estimated mean-reversion κ: {kappa_est:.4e}")
    if kappa_est > 0:
        print("  ✓  Prices mean-revert toward fundamental (κ > 0)")
    else:
        print("  ⚠  Prices NOT mean-reverting over this window")

# ─────────────────────────────────────────────────────────────
# 4.  Return autocorrelation (bid-ask bounce signature)
# ─────────────────────────────────────────────────────────────
print("\n[4/5] Microstructure stylised facts …")

returns = np.diff(mid) / mid[:-1]
if len(returns) > 10:
    ac1 = float(np.corrcoef(returns[:-1], returns[1:])[0, 1])
    ac5 = float(np.corrcoef(returns[:-5], returns[5:])[0, 1]) if len(returns) > 10 else float("nan")
    print(f"  Return autocorrelation lag-1 : {ac1:+.4f}")
    print(f"  Return autocorrelation lag-5 : {ac5:+.4f}")
    if ac1 < -0.01:
        print("  ✓  Negative lag-1 AC — bid-ask bounce present (realistic)")
    else:
        print("  ⚠  Lag-1 AC ≥ 0 — no bid-ask bounce detected")

# Amihud illiquidity proxy  (|return| / volume_per_interval)
trades = mkt.trades
if trades:
    n_trades = len(trades)
    total_vol = sum(t.quantity for t in trades)
    avg_trade_size = total_vol / n_trades
    # Compute signed returns at trade timestamps
    trade_prices = np.array([t.price_cents for t in trades], dtype=float)
    trade_returns = np.abs(np.diff(trade_prices) / trade_prices[:-1])
    trade_qtys = np.array([t.quantity for t in trades[1:]], dtype=float)
    amihud = float(np.mean(trade_returns / np.maximum(trade_qtys, 1)))
    print(f"\n  Trades executed     : {n_trades:,}")
    print(f"  Average trade size  : {avg_trade_size:.1f} shares")
    print(f"  Amihud illiquidity  : {amihud:.6e}")
    if amihud < 1e-4:
        print("  ✓  Low Amihud — market reasonably liquid")
    else:
        print("  ⚠  High Amihud — low liquidity relative to price impact")
else:
    print("  ⚠  No trade attribution data available")

# ─────────────────────────────────────────────────────────────
# 5.  PnL attribution by agent type
# ─────────────────────────────────────────────────────────────
print("\n[5/5] PnL attribution …")

by_category: dict[str, list[int]] = defaultdict(list)
for ag in result.agents:
    by_category[ag.agent_category].append(ag.pnl_cents)

total_pnl = sum(pnl for pnls in by_category.values() for pnl in pnls)
print(f"\n  Total PnL across all agents: ${total_pnl / 100:+,.2f}")
if abs(total_pnl) < STARTING_CASH * 0.01:  # less than 1% of starting cash
    print("  ✓  Approximately zero-sum (wealth conservation holds)")
else:
    print("  ⚠  Significant net PnL — check for oracle pricing leakage")

print()
print(f"  {'Category':<20} {'Agents':>6} {'Total PnL ($)':>14} {'Mean PnL ($)':>13} {'Std PnL ($)':>12}")
print("  " + "-" * 70)
for cat in sorted(by_category):
    pnls = by_category[cat]
    total = sum(pnls)
    mean = np.mean(pnls)
    std = np.std(pnls) if len(pnls) > 1 else 0.0
    print(f"  {cat:<20} {len(pnls):>6} {total/100:>14,.2f} {mean/100:>13,.2f} {std/100:>12,.2f}")

# Agent-level top/bottom
all_agents = sorted(result.agents, key=lambda a: a.pnl_cents, reverse=True)
print(f"\n  Top 5 agents by PnL:")
for ag in all_agents[:5]:
    print(f"    [{ag.agent_type:30s}] ${ag.pnl_cents / 100:+10,.2f}  ({ag.pnl_pct:+.3%})")
print(f"\n  Bottom 5 agents by PnL:")
for ag in all_agents[-5:]:
    print(f"    [{ag.agent_type:30s}] ${ag.pnl_cents / 100:+10,.2f}  ({ag.pnl_pct:+.3%})")

# ─────────────────────────────────────────────────────────────
# Summary verdict
# ─────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("EVALUATION SUMMARY")
print("=" * 70)
issues = []

if sim_warnings:
    issues.append(f"{len(sim_warnings)} structural warning(s) in SimulationResult")
if liq.pct_time_no_bid > 5 or liq.pct_time_no_ask > 5:
    issues.append("Market was one-sided >5% of the time")
if liq.vwap_cents and abs(liq.vwap_cents - R_BAR) / R_BAR > 0.05:
    issues.append(f"VWAP deviates >5% from fundamental (drift={drift_pct:+.2f}%)")
if spread_bps > 50:
    issues.append(f"Mean spread {spread_bps:.1f} bps is unusually wide")
if len(returns) > 10 and ac1 >= 0:
    issues.append("No bid-ask bounce (AC lag-1 ≥ 0)")

if issues:
    print(f"  ⚠  {len(issues)} concern(s) flagged:")
    for iss in issues:
        print(f"      • {iss}")
else:
    print("  OK:  No material concerns -- market dynamics appear consistent with")
    print("     textbook microstructure (price discovery, mean reversion,")
    print("     bid-ask bounce, approximate wealth conservation).")
print()

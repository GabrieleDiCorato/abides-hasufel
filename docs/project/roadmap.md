# ABIDES-NG — Product Roadmap

**Updated**: 2026-05-21
**Baseline**: `v0.1.0` (unreleased on `main`) — 9 registered agent types,
~63 test files, declarative config system (Pydantic, discriminated
unions, composable templates), EventBus + typed sinks (`Memory`, `BZ2`,
`Parquet`) with SHA-256 identity-based seed derivation.

> **Purpose:** Single, prioritised list of unimplemented work. Combines
> the prior feature roadmap with the findings of the May 2026
> independent review.
> **Ordering:** by *release impact*, not by effort. Pre-1.0 blockers
> first; "nice to have" features last.
> **Format:** each item is one numbered entry with motivation, scope,
> and concrete implementation hints. Items inherit status verification
> from a code audit run on the date above — status is noted when not
> simply "pending".

---

## Baseline assessment (one paragraph)

The kernel, declarative config, EventBus + typed sinks, integer-cents
discipline, seeded RNG isolation, and matching-engine correctness
(GTC/DAY/IOC/FOK, stop, modify, replace) are all in good shape. The
roadmap below targets three classes of work: (a) **release blockers**
that should not ship in 1.0 (broken `[gym]` extra, contradictory
`SECURITY.md`, deprecated surfaces, misleading sync-named async API,
silent order rejects, half-implemented iceberg plumbing, drawdown
measured from initial cash instead of peak NAV); (b) **structural code
health** (god-class `TradingAgent`, bloated `OrderBook`, ruff-exempt
bug classes, missing CI gates); and (c) **feature gaps** that limit the
project's usefulness as a market simulator (no fee model, no informed
trader, no multi-symbol templates, no auctions, no LULD, no STP, no
hidden orders, single-fundamental-process oracle). The simulation-
science layer (ensemble runner, stylized-facts scorecard, calibrator)
remains a strong P3 differentiator once the foundations are clean.

---

## Design constraints

These constraints apply to **all** items below.

### Oracle access rules

Only agents that model **informed** trading should access the oracle.
This preserves the informed/uninformed heterogeneity that drives
realistic price discovery.

| Should access oracle | Should NOT |
|---|---|
| `ValueAgent` (Bayesian estimation) | `MomentumAgent` (price-based by definition) |
| Future `InformedTraderAgent` (event-driven) | Execution agents — POV, TWAP, VWAP (benchmark, not alpha) |
| | `NoiseAgent` (axiomatically uninformed) |
| | `MeanReversionAgent` (LOB-only) |

### Exchange vs. agent responsibility

Stop orders, iceberg orders, and circuit breakers belong in the
**matching engine**, not in agent code. Agent-side implementations
introduce inter-message latency that distorts the phenomena they're
meant to model (stop cascades, hidden-liquidity refresh, trading
halts).

---

## P0 — Pre-1.0 release blockers

These items represent commitments the project has already implicitly
made (deprecation warnings, declared extras, security policy, API
contracts) but not fulfilled. Shipping 1.0 without resolving them
locks in technical debt that becomes much more expensive after the
stability boundary.

### 1. Fix the `abides-ng[gym]` extra

**Type:** Packaging | **Effort:** Small–Medium

**Problem:** `pip install abides-ng[gym]` declares an extra that
installs nothing useful — the `abides_gym` source is not included in
the wheel (`hatchling` `force-include` lists only `abides_core` and
`abides_markets`). The README admits the gym adapter has not been
re-validated against the declarative config system since the rewrite.

**Decision required:** bundle, mark unsupported, or remove.

**Implementation hints:**
- Either add `abides-gym/abides_gym` to the wheel's `force-include` list
  in [pyproject.toml](../../pyproject.toml) and re-validate against
  `SimulationConfig`, or remove the `[gym]` extra entirely.
- If kept: depends on item #21 (register gym agents) and a smoke test
  that exercises one gym environment end-to-end in CI.

### 2. Rewrite SECURITY.md

**Type:** Governance | **Effort:** Small

**Problem:** [SECURITY.md](../../SECURITY.md) lists supported versions
`2.6.x — Active` and `< 2.6 — Unsupported`. The public release line
starts at `v0.1.0` (per README and CHANGELOG). The two documents
contradict each other.

**Implementation hints:**
- Rewrite the supported-versions table to reflect the actual versioning
  model (pre-1.0: only `main` is supported; report security issues via
  a stated channel).
- Add a private disclosure address.

### 3. Remove deprecated surfaces before 1.0

**Type:** Cleanup | **Effort:** Medium | **Supersedes:** old roadmap #17

**Problem:** Multiple surfaces are deprecated but still in tree. The
pre-1.0 window is the cheap moment to delete them; afterwards each
removal is a breaking-change event.

Surfaces to remove:

- `MeanRevertingOracleConfig` (deprecated in favour of
  `SparseMeanRevertingOracleConfig`). Keep the low-level
  `MeanRevertingOracle` class if any direct user exists; remove the
  config-system entry point.
- `OrderBook.book_log2` and `OrderBook.history` (deprecated in favour
  of EventBus reads). Remove `_fallback_book_log2`, `_book_log2_cache`,
  `_history_cache`, `_book_log2_warned`, `_history_warned`.
- `Side.legacy_str()` and `TimeInForce.legacy_str()`.
- Procedural `abides-markets/abides_markets/configs/rmsc03.py` and
  `rmsc04.py` — port any unique behaviour into declarative templates
  in `config_system/templates.py`.
- `OrderBook` "standalone-mode" stub fallbacks.

**Implementation hints:**
- One PR per surface; each landing as a `### Removed` entry in the
  changelog.
- Audit `docs/reference/` and notebooks for any remaining references
  before deleting.

### 4. Rename misleading sync-named async API

**Type:** API ergonomics | **Effort:** Small–Medium

**Problem:** `TradingAgent.get_current_spread()`, `get_last_trade()`,
and `get_order_stream()` send a message and return `None`; the data
arrives later in `receive_message()`. The verb says "get". This is the
single largest source of new-user friction (see
[docs/reference/llm-gotchas.md](../reference/llm-gotchas.md) and the
custom-agent guide, which spend pages re-explaining it).

**Implementation hints:**
- Rename to `request_spread()` / `request_last_trade()` /
  `request_order_stream()`.
- Keep the old names as one-release aliases with `DeprecationWarning`
  so the bridge can land before 1.0.
- Update `docs/reference/llm-gotchas.md`, `custom-agent-guide.md`, and
  `copilot-instructions.md` together.

### 5. Replace silent `warnings.warn` rejects with `OrderRejectedMsg`

**Type:** Correctness | **Effort:** Small

**Problem:** Invalid orders (`quantity <= 0`, non-integer price)
trigger `warnings.warn(...)` and disappear. The submitting agent gets
no callback. Real exchanges return a typed reject — strategies that
rely on detecting rejection are silently broken.

**Implementation hints:**
- Define `OrderRejectedMsg(order_id, reason: RejectReason)` with a
  small `RejectReason` enum (`INVALID_QUANTITY`, `INVALID_PRICE`,
  `RISK_LIMIT`, …).
- Send from `ExchangeAgent` order-validation path instead of warning.
- Add a `TradingAgent` default handler that logs and surfaces to the
  agent's `on_order_rejected()` hook.

### 6. Fix bare `raise Exception(...)` in `TradingAgent`

**Type:** Correctness | **Effort:** Trivial

**Problem:** `trading_agent.py` line ~967 raises bare
`Exception("Expected LimitOrder or MarketOrder")`. The rest of the
codebase uses specific exception types.

**Implementation hints:**
- Replace with `TypeError`. Add a regression test that asserts the
  raised type.

### 7. Peak-NAV drawdown enforcement

**Type:** Risk bug fix | **Effort:** Small | **Supersedes:** old roadmap #6

**Problem:** `TradingAgent._check_drawdown()` (called from
`_check_circuit_breaker`) computes drawdown from `starting_cash`, not
from peak NAV. `_peak_nav` is already tracked and updated on every
fill but never used for enforcement. Real risk managers measure from
the high-water mark.

**Implementation hints:**
- Replace `self.starting_cash - mark_to_market()` with
  `self._peak_nav - mark_to_market()` in `_check_drawdown()`.
- `_peak_nav` defaults to `starting_cash`, so agents without fills
  behave identically.
- Update `RiskConfig` docstring to read "max drawdown from peak NAV".
- Add a test where an agent reaches NAV > starting cash, then declines.

### 8. Complete iceberg-order support

**Type:** Exchange | **Effort:** Medium | **Supersedes:** old roadmap #3

**Problem:** `PriceLevel` already separates `visible_orders` and
`hidden_orders` queues, but `LimitOrder` has no `display_quantity`
field and there is no auto-refresh after partial fills. The current
split is half-built and misleading — it suggests support that doesn't
exist.

**Decision required:** finish or remove. Recommendation: finish, because
hidden liquidity is core to modern microstructure and the L1/L2
snapshots otherwise over-represent visible depth.

**Implementation hints:**
- Add `display_quantity: int | None` to `LimitOrder`. When set,
  `PriceLevel` exposes only `display_quantity` shares; on partial fill,
  auto-refresh the visible slice from the hidden reserve.
- Update `get_L1_snapshots()` / `get_L2_snapshots()` to reflect
  visible-only quantities.
- Add `TradingAgent.place_iceberg_order()` convenience method.
- Surface `iceberg_display_quantity` on market-maker configs.

---

## P1 — Structural code health

### 9. Decompose `TradingAgent`

**Type:** Refactor | **Effort:** Large

**Problem:** `TradingAgent` is ~1,600 lines and mixes portfolio
bookkeeping, order tracking, risk enforcement, market-data caching,
14 message dispatch handlers, pre-market detection, mark-to-market,
and the subscription registry. The custom-agent guide warns users
they're inheriting "~1,200 lines of plumbing" — that warning is itself
the diagnosis. Strategies cannot be tested in isolation.

**Implementation hints:**
- Extract `Portfolio` (holdings + cash + mark-to-market),
  `RiskMonitor` (limits + circuit breaker), `MarketDataCache`
  (known_bids/asks + last_trade + exchange_ts + daily_close), and
  `OrderTracker` (order_id ↔ Order map + lifecycle handlers).
- Compose them into a thin `TradingAgent` adapter.
- Each component should be unit-testable with a stub `BaseAgent`.
- Migrate built-in agents first; preserve `TradingAgent` as a façade
  during transition.

### 10. Slim `OrderBook`

**Type:** Refactor | **Effort:** Medium

**Problem:** `OrderBook` (~1,200 lines) mixes matching, persistence,
market-data publication, deprecated-property caches, and
"standalone-mode" stub fallbacks. The standalone fallbacks and
deprecated caches go in #3; the remaining concerns should be split:

- `MatchingEngine` — pure matching logic.
- `OrderBookPublisher` — L1/L2 publish dedup, EventBus emission.
- `OrderBookSnapshot` — read-only view passed to subscribers.

### 11. Enforce the `agents[i].id == i` invariant

**Type:** Correctness | **Effort:** Small

**Problem:** Documented in `Kernel.__init__` but enforced only at
construction. Any code path that mutates `kernel.agents` post-init
silently corrupts every parallel array
(`_agent_current_times`, `agent_computation_delays`).

**Implementation hints:**
- Store as a `tuple[Agent, ...]` or wrap the list behind a property
  setter that re-validates the invariant.
- Add a regression test that mutates the list and asserts the error.

### 12. Address ruff suppressions

**Type:** Cleanup | **Effort:** Medium

**Problem:** [pyproject.toml](../../pyproject.toml) suppresses `B006`
(mutable default args), `B008` (function call in default arg), `B028`
(`warnings.warn` without `stacklevel`), and `N802/N803/N806` (PEP-8
naming). Each is a real bug class. Suppressions inherited from upstream
should be retired before 1.0.

**Implementation hints:**
- Address one rule per PR. `B006`/`B008` are mechanical and high-yield.
- `N80x` requires renaming legacy attributes/methods; coordinate with
  #4 (API rename) where overlap exists.

### 13. Resolve editable-install footgun

**Type:** Developer UX | **Effort:** Small–Medium

**Problem:** Hatchling's `force-include` copies `abides_core` into
`site-packages` rather than linking it, so contributors must run
`uv sync --reinstall-package abides-ng` after every kernel edit
([CONTRIBUTING.md](../../CONTRIBUTING.md)). Every new contributor
hits this.

**Implementation hints:**
- Investigate `hatch-build.dev-mode-dirs` to map `abides_core` as a
  link in editable installs.
- Alternative: restructure so `abides_core` ships as its own wheel
  declared in workspace dependencies, removing the `force-include`
  altogether.

### 14. Reconcile async-model documentation drift

**Type:** Docs | **Effort:** Small

**Problem:** [.github/copilot-instructions.md](../../.github/copilot-instructions.md),
[docs/reference/custom-agent-guide.md](../reference/custom-agent-guide.md),
and [docs/reference/llm-gotchas.md](../reference/llm-gotchas.md) each
restate the async-model rules. They will diverge.

**Implementation hints:**
- Pick `llm-gotchas.md` as canonical. The other two link to it.

---

## P1 — Quality gates

### 15. Lift coverage `fail_under` above 0

**Type:** CI | **Effort:** Trivial

**Problem:** `[tool.coverage.report]` sets `fail_under = 0`. Coverage
is measured but not enforced.

**Implementation hints:**
- Run a coverage report on `main`, set `fail_under` slightly below
  the measured value as a regression floor. Raise over time.

### 16. Wire one benchmark into CI as a regression gate

**Type:** CI | **Effort:** Medium

**Problem:** [benchmarks/README.md](../../benchmarks/README.md)
concedes: "scaffolding, not infrastructure". Throughput, memory, and
`parse_logs_df` latency regress silently.

**Implementation hints:**
- Pick `headless_sim_throughput_no_sinks.py` as the canonical CI
  benchmark. Persist a baseline; fail the build on >X% regression with
  generous tolerance for runner variance.
- Run only on `main` and on PRs that touch `abides-core` or
  `abides-markets`.

---

## P2 — Governance & onboarding

### 17. Add `CODE_OF_CONDUCT.md`, issue templates, and `RELEASE.md`

**Type:** Governance | **Effort:** Small

**Problem:** None of these exist. README states "solo-maintained" and
welcomes co-maintainers but supplies no contribution scaffolding.

**Implementation hints:**
- Use the Contributor Covenant for the code of conduct.
- Issue templates: `bug_report.md`, `feature_request.md`.
- `RELEASE.md`: codify the steps in
  [docs/project/release-process.md](release-process.md) into a checklist.

### 18. Ship at least one multi-symbol template

**Type:** Templates | **Effort:** Small

**Problem:** Every shipped template uses one `ticker`. Multi-symbol is
plumbed through `OrderBook` but not exercised. A user looking to
prototype pairs trading or cross-asset execution today must wire it
themselves and discovers the gap only after committing time.

**Implementation hints:**
- Add a two-symbol template (e.g. `rmsc04_two_symbol`) with separate
  exchange entries, independent oracle paths (until item #28 ships
  correlation), and at least one cross-symbol-aware agent (could be a
  pair of independent value agents on different symbols as a smoke
  test).

---

## P3 — Market microstructure features

These items close realism gaps that matter most for the execution-
quality research use case the project most directly serves.

### 19. Fee / commission / rebate model

**Type:** Feature (new) | **Effort:** Medium

**Problem:** No exchange fees, no commissions, no maker/taker rebates.
For execution-strategy research (POV/TWAP/VWAP, market-impact
templates), slippage realism without cost realism is half the picture.

**Implementation hints:**
- `FeeSchedule(maker_bps, taker_bps, fixed_per_share, fixed_per_order)`
  attached to `ExchangeAgent` config.
- Apply in `OrderExecutedMsg` PnL accounting in `TradingAgent`.
- Emit a `FeeAppliedMsg` event so the metrics layer can attribute
  costs.
- Templates: a default `us_equity_maker_taker` schedule and a
  zero-fee schedule preserving current behaviour.

### 20. Self-trade prevention (STP)

**Type:** Exchange | **Effort:** Small | **Supersedes:** old roadmap #10

**Problem:** The matching engine does not check whether the aggressive
and passive sides belong to the same agent. Real exchanges reject or
cancel.

**Implementation hints:**
- In `OrderBook.execute_trade()`, check
  `incoming_order.agent_id != resting_order.agent_id`.
- Add `self_trade_prevention: Literal["cancel_newest", "cancel_oldest", "none"]`
  to exchange config (default `"cancel_newest"`).
- Emit `OrderRejectedMsg(reason=SELF_TRADE)` — depends on #5.

### 21. Exchange-level LULD / circuit breakers

**Type:** Exchange | **Effort:** Medium | **Supersedes:** old roadmap #9

**Problem:** No exchange-wide trading halts. Tail-risk dynamics (flash
crashes) cannot be modelled faithfully. MiFID II and Reg NMS both
require these in real venues.

**Implementation hints:**
- `luld_band_pct: float | None` on exchange config. Reference price
  computed at open (or rolling window).
- Reject orders outside bands; halt for configurable duration when
  consecutive rejections exceed threshold.
- `TradingHaltMsg` to all subscribed agents.

### 22. Opening / closing auctions

**Type:** Exchange | **Effort:** Large | **Supersedes:** old roadmap #15

**Problem:** Market opens by seeding price from oracle directly. No
call-auction mechanism. Opening/closing auctions handle 15–20% of
volume on major exchanges.

**Implementation hints:**
- `AuctionPhase` state in `ExchangeAgent` with a separate order
  collection period.
- At auction end, compute clearing price via maximum-volume matching.
- Transition to continuous trading after open auction; run close
  auction before market close.
- Significant: affects exchange state machine, message flow, all agent
  timing. Coordinate with #5 (rejection messaging).

### 23. Hidden / post-only / MOO / MOC / peg order types

**Type:** Exchange | **Effort:** Medium

**Problem:** Beyond the iceberg work in #8, modern US-equity
microstructure relies heavily on post-only flags, market-on-open and
market-on-close, and peg orders. Strategies that depend on them cannot
be modelled.

**Implementation hints:**
- Per-flag rollout: post-only first (rejects if it would cross), then
  MOO/MOC (depends on #22), then peg (mid/primary/market).

### 24. Latency-model calibration preset

**Type:** Realism | **Effort:** Medium

**Problem:** Latency is parametric (`deterministic`, `uniform`) with
no shipped fit to real venue profiles. "High-fidelity" claims should
be backed by at least one calibrated preset.

**Implementation hints:**
- Fit a `LatencyModelConfig` against published Nasdaq or NYSE Itch
  latency distributions; ship as `LatencyPreset.NASDAQ_TYPICAL`.
- Document the source dataset and fit methodology.

---

## P4 — Agent & oracle features

### 25. Oracle event subscription API

**Type:** Architecture | **Effort:** Large | **Unlocks:** #26 (InformedTraderAgent), AMM oracle anchor (#33) | **Supersedes:** old roadmap #1

**Problem:** Oracle generates megashocks and fundamental-value jumps,
but agents can only observe by polling `observe_price()`. No mechanism
for agents to *subscribe* to discrete oracle events (earnings releases,
regime shifts, shocks) with configurable delay and noise.

**Motivation:** Adverse selection — the primary microstructure
phenomenon driving bid-ask spread dynamics — cannot be modelled
without informed agents reacting to fundamental events before prices
adjust. Single largest realism gap remaining.

**Implementation hints:**
- `OracleEventMsg(event_type, symbol, magnitude, oracle_time_ns)`.
- `subscribe_to_events(agent_id, delay_ns, noise_std)` on the oracle ABC.
- `SparseMeanRevertingOracle` already generates megashocks internally
  — route them through the kernel message system with per-subscriber
  delay/noise injection.
- Config: `oracle_event_delay` and `oracle_event_noise` on agent
  configs that opt in.

### 26. `InformedTraderAgent`

**Type:** New agent | **Effort:** Medium | **Depends on:** #25 | **Supersedes:** old roadmap #2

**Problem:** No agent models informed trading on private fundamental
information. `ValueAgent` estimates continuously; it does not react to
discrete shocks.

**Motivation:** Informed traders are the counterparty market makers
fear. Without them, simulated quoted spreads are narrower than reality
and `compute_adverse_selection()` has no causal driver.

**Implementation hints:**
- Subscribe to oracle events via #25.
- On event: submit aggressive limit/market orders proportional to
  `(event_magnitude × aggressiveness)` with configurable reaction
  delay.
- Config: `aggressiveness`, `reaction_delay`, `position_limit`,
  `symbols`. Register `"informed_trader"` (category: strategy).
- Acceptance test: verify adverse-selection bps increase when present
  vs. baseline.

### 27. `MomentumAgent` exit logic

**Type:** Agent improvement | **Effort:** Small | **Supersedes:** old roadmap #4

**Problem:** Enters on MA crossover but has no exit — rides trends
indefinitely. Overstates trend-following impact.

**Implementation hints:**
- Add `trailing_stop_pct: float | None`, `profit_target_pct: float | None`,
  `exit_on_reversal: bool` to `MomentumAgentConfig`.
- Track entry price per position. On each wakeup, check exit
  conditions before entry signals. Defaults preserve current behaviour
  (`None` / `False`).

### 28. POV limit-order mode + urgency

**Type:** Agent improvement | **Effort:** Small–Medium | **Status:** PARTIAL — `order_style` exists with `["market", "ioc_limit"]`; missing `"adaptive"` and `urgency_curve` | **Supersedes:** old roadmap #5

**Problem:** `BaseSlicingExecutionAgent` supports market and IOC limit
but no adaptive mode and no urgency curve. Market-only POV overstates
impact.

**Implementation hints:**
- Add `"adaptive"` value to `order_style`: start with limit, switch to
  market as urgency rises toward deadline.
- `urgency_curve: Literal["linear", "convex"] | None` for time-varying
  participation.

### 29. Correlated multi-symbol oracle

**Type:** Oracle | **Effort:** Medium | **Unlocks:** #30 | **Supersedes:** old roadmap #11

**Problem:** Each symbol's OU process is independent. No correlation
structure between symbols. Required for portfolio stress tests and
cross-asset strategies.

**Implementation hints:**
- Accept correlation matrix in `SparseMeanRevertingOracleConfig`.
- Cholesky-decompose; draw correlated shocks at each step.
- Fall back to independent when no matrix supplied.

### 30. `PairsArbitrageAgent`

**Type:** New agent | **Effort:** Medium | **Depends on:** #29 | **Supersedes:** old roadmap #12

**Problem:** No multi-symbol agent exists.

**Implementation hints:**
- Track spread z-score between two configured symbols. Enter on
  `z > entry_threshold`; exit on `|z| < exit_threshold`.
- First agent to subscribe to two exchanges. Config:
  `symbol_pair: tuple[str, str]`.
- Register `"pairs_arbitrage"` (category: strategy).

### 31. `HFTAgent`

**Type:** New agent | **Effort:** Medium | **Supersedes:** old roadmap #13

**Problem:** No agent models HFT — queue priority exploitation,
latency-sensitive cancellation, speed-based adverse selection.

**Implementation hints:**
- Subscribe to L1. On price change: cancel stale quotes immediately,
  re-quote at new best.
- Exploit latency advantage (lower `computation_delay`).
- Register `"hft"` (category: market_maker).

### 32. `ImplementationShortfallAgent`

**Type:** New agent | **Effort:** Medium | **Supersedes:** old roadmap #8

**Problem:** No agent implements Almgren-Chriss optimal execution.
IS is the standard institutional benchmark.

**Implementation hints:**
- Subclass `BaseSlicingExecutionAgent`. Closed-form Almgren-Chriss
  trajectory given `(target_qty, risk_aversion, volatility_estimate,
  impact_coefficient)`.
- Slice sizes front-loaded when risk-averse, back-loaded when
  impact-averse.
- Register `"implementation_shortfall"` (category: execution).

### 33. Optional AMM fundamental anchor

**Type:** Agent enhancement | **Effort:** Small | **Depends on:** #25 (for event-driven update) | **Supersedes:** old roadmap #14

**Problem:** `AdaptiveMarketMakerAgent` quotes around the LOB mid only.
No option to blend in fundamental value.

**Implementation hints:**
- `oracle_anchor_weight: float = 0.0` on `AdaptiveMarketMakerConfig`.
  At 0.0, behaviour unchanged.
- When > 0, query oracle and blend `alpha × oracle + (1-alpha) × LOB mid`.
- Document the oracle-access exemption (this is informed market
  making).

### 34. CSV / Parquet data providers

**Type:** Data infrastructure | **Effort:** Small | **Supersedes:** old roadmap #7

**Problem:** `ExternalDataOracle` supports `BatchDataProvider` but
only ships `DataFrameProvider`. `CsvProvider` is a stub example;
`ParquetProvider` is absent.

**Implementation hints:**
- Real `CsvProvider(path, symbol_col, time_col, price_col)`. Validate
  columns and integer-cents convention at construction.
- `ParquetProvider(path, symbol_col, time_col, price_col)` backed by
  `pd.read_parquet()`.
- Both implement `BatchDataProvider`.

---

## P5 — Simulation science

Foundation for understanding, validating, and calibrating simulations.
Dependency-ordered: #35 is the foundation; #36–#39 build on it.

```
#35 ensemble runner
    ├── #36 stylized facts scorecard
    ├── #37 sensitivity analysis
    └── #39 agent impact attribution

#35 + #36 + #37 → #38 scenario calibrator
```

Items live in `abides-markets/abides_markets/simulation/science.py`
(or a `science/` subpackage).

### 35. Multi-seed ensemble runner

**Type:** Infrastructure | **Effort:** Small | **Supersedes:** old roadmap #18

**Implementation hints:**
- `run_ensemble(config, n_seeds, n_jobs=-1, profile=ResultProfile.SUMMARY) -> EnsembleResult`.
- `multiprocessing.Pool` (per
  [docs/reference/parallel-simulation.md](../reference/parallel-simulation.md)).
- Each worker: `run_simulation(config.with_seed(seed))`.
- `EnsembleResult` wraps a `pd.DataFrame` (row per seed, column per
  scalar metric); `.mean()`, `.std()`, `.ci(alpha=0.05)`.
- RNG safety: `SimulationConfig.with_seed(seed)` preserves master-seed
  isolation.

### 36. Stylized facts scorecard

**Type:** Validation | **Effort:** Medium | **Depends on:** #35 | **Supersedes:** old roadmap #19

**Implementation hints:**
- `score_stylized_facts(ensemble: EnsembleResult) -> StyleFacts`
  computing: return kurtosis > 3, near-zero return autocorrelation at
  lags 1–10, positive autocorrelation of absolute returns at lags 1–20,
  negative volume–spread correlation, right-skewed spread distribution
  with power-law tail.
- Pass/fail flag + numeric score per fact. `composite_score` is
  weighted mean.
- Requires `ResultProfile.QUANT` for return series; use L1 mid-price.
- `StyleFacts` is `model_dump()`-able.

### 37. Parameter sensitivity analysis

**Type:** Analysis | **Effort:** Medium | **Depends on:** #35 | **Supersedes:** old roadmap #20

**Implementation hints:**
- `sensitivity_analysis(base_config, param_space, metric_keys, n_samples, n_seeds) -> SensitivityResult`.
- `param_space`: dotted paths → `(low, high)`.
- Latin Hypercube Sampling (`scipy.stats.qmc.LatinHypercube`). For each
  sample: patch config, run ensemble, record metric means.
- First-order indices = Pearson correlation across samples.
- `SensitivityResult.matrix`: `DataFrame[param × metric]`; `.plot_heatmap()`.

### 38. Scenario calibrator

**Type:** Optimisation | **Effort:** Large | **Depends on:** #35, #36, #37 | **Supersedes:** old roadmap #21

**Implementation hints:**
- `calibrate(base_config, param_space, target, n_seeds, optimizer) -> CalibrationResult`.
- Two modes: stylized-facts (`target` = `StyleFacts` score) or
  historical (`target` = empirical metric vector from real L1/L2 via
  `BatchDataProvider`).
- Objective: `loss(params) = ensemble_mean_metric_distance(patched, target)`.
- Optimizers: `"nelder_mead"` (fast) or `"differential_evolution"`
  (global) via `scipy.optimize`.
- Recommended workflow (document): use #37 to reduce `param_space` to
  top-K parameters before running.
- `CalibrationResult`: best config, best loss, convergence trace,
  `StyleFacts` of calibrated result.

### 39. Agent impact attribution

**Type:** Analysis | **Effort:** Small | **Depends on:** #35 | **Supersedes:** old roadmap #22

**Implementation hints:**
- `agent_impact(base_config, metric_keys, n_seeds) -> ImpactResult`.
- For each agent group: run with count=0 and count/2 vs. baseline.
- `ImpactResult.delta_matrix`: `DataFrame[agent_group × metric]`
  absolute and relative deltas. `.rank_by_impact(metric)`.
- "Removing" = patch count to 0 via the same mechanism as #37.

---

## P6 — Housekeeping

### 40. Register gym agents in the config system

**Type:** Housekeeping | **Effort:** Small | **Depends on:** #1 (gym extra resolution) | **Supersedes:** old roadmap #16

**Problem:** `CoreBackgroundAgent` and `FinancialGymAgent` are not
registered via `@register_agent`. Users building gym environments via
the declarative config system cannot reference them.

**Implementation hints:**
- Add `@register_agent` decorators in `abides_gym`, or add a
  `gym_registrations.py` that conditionally imports from `abides_gym`.
- Only meaningful once #1 decides the fate of the `[gym]` extra.

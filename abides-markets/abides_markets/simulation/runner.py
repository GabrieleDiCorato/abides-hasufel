"""Runner functions: run_simulation() and run_batch().

Both are pure functions — no side effects beyond writing ABIDES log files to disk.

Thread / process safety
-----------------------
* ``run_simulation`` is safe to call from multiple threads as long as each
  invocation uses a distinct ``log_dir`` (auto-assigned via UUID when omitted).
* ``run_batch`` uses ``multiprocessing`` (spawn on Windows, fork on POSIX) and
  is therefore also safe from a GIL standpoint.  Each worker process compiles
  its own ``SimulationConfig`` → runtime dict independently.
* The returned :class:`~abides_markets.simulation.SimulationResult` objects are
  frozen Pydantic models with read-only numpy arrays — inherently thread-safe.

Custom extractors in run_batch
------------------------------
Extractor objects must be **picklable** to be sent to worker processes.
:class:`~abides_markets.simulation.FunctionExtractor` wrapping a ``lambda``
is **not** picklable on most Python implementations.  Use
:class:`~abides_markets.simulation.BaseResultExtractor` subclasses instead,
or top-level functions wrapped in ``FunctionExtractor``.
"""

from __future__ import annotations

import multiprocessing
import os
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd

from abides_core.abides import run as abides_run
from abides_core.utils import parse_logs_df
from abides_markets.agents.exchange_agent import ExchangeAgent
from abides_markets.agents.trading_agent import TradingAgent
from abides_markets.config_system import compile as compile_config
from abides_markets.config_system.models import SimulationConfig

from .extractors import ResultExtractor
from .profiles import ResultProfile
from .result import (
    AgentData,
    ExecutionMetrics,
    L1Close,
    L1Snapshots,
    L2Snapshots,
    LiquidityMetrics,
    MarketSummary,
    SimulationMetadata,
    SimulationResult,
    TradeAttribution,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_simulation(
    config: SimulationConfig,
    *,
    profile: ResultProfile = ResultProfile.SUMMARY,
    log_dir: str | None = None,
    extractors: list[ResultExtractor] | None = None,
) -> SimulationResult:
    """Run a single simulation and return a typed, immutable result.

    Parameters
    ----------
    config:
        A validated :class:`~abides_markets.config_system.SimulationConfig`.
        Build one with
        :class:`~abides_markets.config_system.SimulationBuilder` or construct
        it directly.
    profile:
        Controls which data is extracted.  Defaults to
        :attr:`~abides_markets.simulation.ResultProfile.SUMMARY` (kilobyte-scale
        output).  Use :attr:`~abides_markets.simulation.ResultProfile.QUANT`
        to add L1/L2 time-series, or
        :attr:`~abides_markets.simulation.ResultProfile.FULL` to also include
        the raw agent log DataFrame.
    log_dir:
        Directory path where ABIDES writes its compressed log files.
        Auto-assigned to a UUID-based subdirectory when ``None`` — this avoids
        the timestamp-collision hazard present in the default ABIDES behaviour.
    extractors:
        Optional list of :class:`~abides_markets.simulation.ResultExtractor`
        plugins.  Each extractor receives the raw ``end_state`` dict and
        contributes a value to ``SimulationResult.extensions``.

    Returns
    -------
    SimulationResult
        Frozen, thread-safe value object.  No live agent references are
        retained after this function returns.
    """
    runtime = compile_config(config)
    effective_log_dir = log_dir if log_dir is not None else uuid4().hex

    end_state = abides_run(
        runtime,
        log_dir=effective_log_dir,
        kernel_random_state=runtime["random_state_kernel"],
    )

    return _extract_result(end_state, config, runtime, profile, extractors or [])


def run_batch(
    configs: list[SimulationConfig],
    *,
    profile: ResultProfile = ResultProfile.SUMMARY,
    n_workers: int | None = None,
    extractors: list[ResultExtractor] | None = None,
    log_dir_prefix: str | None = None,
) -> list[SimulationResult]:
    """Run multiple simulations in parallel and return results in input order.

    Each simulation runs in a separate process (``multiprocessing``).  Results
    are collected and returned in the same order as *configs*.

    Parameters
    ----------
    configs:
        List of :class:`~abides_markets.config_system.SimulationConfig` objects.
        Each should have an explicit integer seed (not ``"random"``) for
        reproducibility.
    profile:
        Extraction profile applied to every simulation.
    n_workers:
        Number of worker processes.  Defaults to ``os.cpu_count()``.
    extractors:
        Extractor plugins applied in every worker.  Must be **picklable**
        (avoid lambdas; use top-level functions or
        :class:`~abides_markets.simulation.BaseResultExtractor` subclasses).
    log_dir_prefix:
        Optional prefix for worker log directories.
        Worker *i* writes to ``{prefix}_{i}``; a UUID is appended when
        ``None`` to guarantee uniqueness.

    Returns
    -------
    list[SimulationResult]
        One result per input config, in the same order.
    """
    effective_workers = n_workers if n_workers is not None else os.cpu_count()

    # Build arg tuples; each worker gets a unique log dir
    args = [
        (
            cfg,
            profile,
            f"{log_dir_prefix}_{i}" if log_dir_prefix else uuid4().hex,
            extractors or [],
        )
        for i, cfg in enumerate(configs)
    ]

    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(processes=effective_workers) as pool:
        results = pool.starmap(_worker, args)

    return results


# ---------------------------------------------------------------------------
# Worker (top-level so it is picklable on Windows/spawn)
# ---------------------------------------------------------------------------


def _worker(
    config: SimulationConfig,
    profile: ResultProfile,
    log_dir: str,
    extractors: list[ResultExtractor],
) -> SimulationResult:
    """Single-simulation worker for ``run_batch``.

    Compiles the config fresh inside the worker to avoid serialising numpy
    ``RandomState`` objects across the process boundary.
    """
    runtime = compile_config(config)
    end_state = abides_run(
        runtime,
        log_dir=log_dir,
        kernel_random_state=runtime["random_state_kernel"],
    )
    return _extract_result(end_state, config, runtime, profile, extractors)


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------


def _extract_result(
    end_state: dict[str, Any],
    config: SimulationConfig,
    runtime: dict[str, Any],
    profile: ResultProfile,
    extractors: list[ResultExtractor],
) -> SimulationResult:
    """Build a :class:`SimulationResult` from a raw ABIDES ``end_state`` dict."""

    agents_list = end_state["agents"]

    # -- Identify exchange and trading agents ---------------------------------
    exchange: ExchangeAgent = agents_list[0]  # ExchangeAgent is always id=0
    if not isinstance(exchange, ExchangeAgent):
        raise RuntimeError(
            "Expected agents[0] to be ExchangeAgent but got "
            f"{type(exchange).__name__}"
        )

    trading_agents: list[TradingAgent] = [
        a for a in agents_list[1:] if isinstance(a, TradingAgent)
    ]

    symbols = list(exchange.order_books.keys())

    # -- Simulation metadata --------------------------------------------------
    seed = runtime["seed"]
    sim_start_ns = int(runtime["start_time"])
    sim_end_ns = int(runtime["stop_time"])
    wall_clock_s = (
        end_state.get("kernel_event_queue_elapsed_wallclock") or pd.Timedelta(0)
    ).total_seconds()

    config_snapshot = _safe_config_snapshot(config)

    metadata = SimulationMetadata(
        seed=seed,
        tickers=symbols,
        sim_start_ns=sim_start_ns,
        sim_end_ns=sim_end_ns,
        wall_clock_elapsed_s=wall_clock_s,
        config_snapshot=config_snapshot,
    )

    # -- Per-symbol market data -----------------------------------------------
    markets: dict[str, MarketSummary] = {}
    for symbol in symbols:
        order_book = exchange.order_books[symbol]
        book_log2 = order_book.book_log2

        l1_close = _extract_l1_close(book_log2)
        liquidity = _extract_liquidity(exchange, symbol, order_book)

        l1_series: L1Snapshots | None = None
        l2_series: L2Snapshots | None = None
        trades: list[TradeAttribution] | None = None

        if ResultProfile.L1_SERIES in profile:
            l1_series = _extract_l1_series(book_log2)

        if ResultProfile.L2_SERIES in profile:
            l2_series = _extract_l2_series(book_log2)

        if ResultProfile.TRADE_ATTRIBUTION in profile:
            trades = _extract_trades(order_book)

        markets[symbol] = MarketSummary(
            symbol=symbol,
            l1_close=l1_close,
            liquidity=liquidity,
            l1_series=l1_series,
            l2_series=l2_series,
            trades=trades,
        )

    # -- Per-agent PnL --------------------------------------------------------
    agent_data: list[AgentData] = []
    if ResultProfile.AGENT_PNL in profile:
        exchange_last_trades = {
            sym: ob.last_trade for sym, ob in exchange.order_books.items()
        }
        # Collect per-symbol liquidity for execution metrics
        symbol_liquidity = {sym: mkt.liquidity for sym, mkt in markets.items()}
        for agent in trading_agents:
            agent_data.append(
                _extract_agent_data(agent, exchange_last_trades, symbol_liquidity)
            )

    # -- Agent logs -----------------------------------------------------------
    logs_df: pd.DataFrame | None = None
    if ResultProfile.AGENT_LOGS in profile:
        raw_df = parse_logs_df(end_state)
        # Ensure the four guaranteed base columns are correctly typed
        raw_df["EventTime"] = pd.array(raw_df["EventTime"], dtype="Int64")
        raw_df["agent_id"] = pd.array(raw_df["agent_id"], dtype="Int64")
        raw_df["EventType"] = raw_df["EventType"].astype(str)
        raw_df["agent_type"] = raw_df["agent_type"].astype(str)
        logs_df = raw_df

    # -- Custom extractors ----------------------------------------------------
    extensions: dict[str, Any] = {}
    for extractor in extractors:
        extensions[extractor.key] = extractor.extract(end_state)

    return SimulationResult(
        metadata=metadata,
        markets=markets,
        agents=agent_data,
        logs=logs_df,
        extensions=extensions,
        profile=profile,
    )


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _extract_l1_close(book_log2: list[dict]) -> L1Close:
    """Return an L1Close from the last entry in book_log2, or empty if no log."""
    if not book_log2:
        return L1Close(time_ns=0, bid_price_cents=None, ask_price_cents=None)

    last = book_log2[-1]
    time_ns = int(last["QuoteTime"])

    bids = last["bids"]
    asks = last["asks"]

    bid_price = int(bids[0][0]) if len(bids) > 0 else None
    ask_price = int(asks[0][0]) if len(asks) > 0 else None

    return L1Close(
        time_ns=time_ns, bid_price_cents=bid_price, ask_price_cents=ask_price
    )


def _extract_liquidity(
    exchange: ExchangeAgent, symbol: str, order_book: Any
) -> LiquidityMetrics:
    """Build LiquidityMetrics from MetricTracker and order book state."""
    has_trackers = (
        hasattr(exchange, "metric_trackers") and symbol in exchange.metric_trackers
    )
    if has_trackers:
        mt = exchange.metric_trackers[symbol]
        pct_no_bid = float(mt.pct_time_no_liquidity_bids)
        pct_no_ask = float(mt.pct_time_no_liquidity_asks)
        total_vol = int(mt.total_exchanged_volume)
        last_trade = int(mt.last_trade) if mt.last_trade else None
    else:
        pct_no_bid = 0.0
        pct_no_ask = 0.0
        total_vol = 0
        last_trade = None

    # If MetricTracker didn't have a last_trade, fall back to order book
    if last_trade is None and order_book.last_trade:
        last_trade = int(order_book.last_trade)

    # Compute VWAP from order book history (EXEC entries)
    vwap_cents: int | None = None
    if hasattr(order_book, "history"):
        total_value = 0
        total_qty = 0
        for entry in order_book.history:
            if entry.get("type") == "EXEC" and entry.get("price") is not None:
                price = int(entry["price"])
                qty = int(entry["quantity"])
                total_value += price * qty
                total_qty += qty
        if total_qty > 0:
            vwap_cents = total_value // total_qty

    return LiquidityMetrics(
        pct_time_no_bid=pct_no_bid,
        pct_time_no_ask=pct_no_ask,
        total_exchanged_volume=total_vol,
        last_trade_cents=last_trade,
        vwap_cents=vwap_cents,
    )


def _extract_trades(order_book: Any) -> list[TradeAttribution]:
    """Build a list of :class:`TradeAttribution` from EXEC entries in order book history."""
    trades: list[TradeAttribution] = []
    if not hasattr(order_book, "history"):
        return trades
    for entry in order_book.history:
        if entry.get("type") != "EXEC":
            continue
        price = entry.get("price")
        if price is None:
            continue
        trades.append(
            TradeAttribution(
                time_ns=int(entry["time"]),
                passive_agent_id=int(entry["agent_id"]),
                aggressive_agent_id=int(entry["oppos_agent_id"]),
                side=str(entry["side"]),
                price_cents=int(price),
                quantity=int(entry["quantity"]),
            )
        )
    return trades


def _extract_l1_series(book_log2: list[dict]) -> L1Snapshots:
    """Build L1Snapshots from book_log2."""
    if not book_log2:
        empty = np.array([], dtype=np.int64)
        empty_obj = np.array([], dtype=object)
        return L1Snapshots(
            times_ns=empty,
            bid_prices=empty_obj,
            bid_quantities=empty_obj,
            ask_prices=empty_obj,
            ask_quantities=empty_obj,
        )

    times, bid_prices, bid_quantities, ask_prices, ask_quantities = [], [], [], [], []

    for entry in book_log2:
        times.append(int(entry["QuoteTime"]))
        bids = entry["bids"]
        asks = entry["asks"]

        if len(bids) > 0:
            bid_prices.append(int(bids[0][0]))
            bid_quantities.append(int(bids[0][1]))
        else:
            bid_prices.append(None)
            bid_quantities.append(None)

        if len(asks) > 0:
            ask_prices.append(int(asks[0][0]))
            ask_quantities.append(int(asks[0][1]))
        else:
            ask_prices.append(None)
            ask_quantities.append(None)

    return L1Snapshots(
        times_ns=np.array(times, dtype=np.int64),
        bid_prices=np.array(bid_prices, dtype=object),
        bid_quantities=np.array(bid_quantities, dtype=object),
        ask_prices=np.array(ask_prices, dtype=object),
        ask_quantities=np.array(ask_quantities, dtype=object),
    )


def _extract_l2_series(book_log2: list[dict]) -> L2Snapshots:
    """Build L2Snapshots directly from book_log2.

    Reads the *already-sparse* ``bids`` and ``asks`` arrays from each snapshot
    (populated by ``get_l2_bid_data()`` / ``get_l2_ask_data()`` which filter
    out empty price levels).  No zero-padding is applied.
    """
    if not book_log2:
        return L2Snapshots(times_ns=np.array([], dtype=np.int64), bids=[], asks=[])

    times = []
    bids_list: list[list[tuple[int, int]]] = []
    asks_list: list[list[tuple[int, int]]] = []

    for entry in book_log2:
        times.append(int(entry["QuoteTime"]))
        bids_list.append([(int(p), int(q)) for p, q in entry["bids"]])
        asks_list.append([(int(p), int(q)) for p, q in entry["asks"]])

    return L2Snapshots(
        times_ns=np.array(times, dtype=np.int64),
        bids=bids_list,
        asks=asks_list,
    )


def _extract_agent_data(
    agent: TradingAgent,
    exchange_last_trades: dict[str, int],
    symbol_liquidity: dict[str, LiquidityMetrics],
) -> AgentData:
    """Build AgentData for a single TradingAgent.

    Uses exchange last-trade prices for mark-to-market to avoid
    calling ``agent.mark_to_market()`` (which has logging side-effects and
    can raise ``KeyError`` if the agent never observed a trade).
    """
    holdings = dict(agent.holdings)
    cash = holdings.get("CASH", 0)

    mtm = cash + agent.basket_size * agent.nav_diff
    for symbol, shares in holdings.items():
        if symbol == "CASH":
            continue
        price = exchange_last_trades.get(symbol)
        if price is None:
            price = agent.last_trade.get(symbol, 0)
        mtm += price * shares

    starting = agent.starting_cash
    pnl = mtm - starting
    pnl_pct = (pnl / starting * 100.0) if starting != 0 else 0.0

    exec_metrics = _extract_execution_metrics(agent, symbol_liquidity)

    return AgentData(
        agent_id=agent.id,
        agent_type=agent.type or type(agent).__name__,
        agent_name=agent.name or f"agent_{agent.id}",
        final_holdings=holdings,
        starting_cash_cents=starting,
        mark_to_market_cents=mtm,
        pnl_cents=pnl,
        pnl_pct=pnl_pct,
        execution_metrics=exec_metrics,
    )


def _safe_config_snapshot(config: SimulationConfig) -> dict[str, Any]:
    """Return a JSON-serialisable subset of SimulationConfig."""
    return {
        "ticker": config.market.ticker,
        "date": config.market.date,
        "start_time": config.market.start_time,
        "end_time": config.market.end_time,
        "seed": config.simulation.seed,
        "log_level": config.simulation.log_level,
        "agent_groups": {
            name: {"count": g.count, "enabled": g.enabled}
            for name, g in config.agents.items()
        },
    }


def _extract_execution_metrics(
    agent: TradingAgent,
    symbol_liquidity: dict[str, LiquidityMetrics],
) -> ExecutionMetrics | None:
    """Build ExecutionMetrics for execution-category agents (duck-typed).

    Returns ``None`` for non-execution agents or when required attributes
    are missing.
    """
    # Duck-type: execution agents expose execution_history, quantity, executed_quantity
    execution_history: list[dict] | None = getattr(agent, "execution_history", None)
    target_qty: int | None = getattr(agent, "quantity", None)
    filled_qty: int | None = getattr(agent, "executed_quantity", None)
    if execution_history is None or target_qty is None or filled_qty is None:
        return None

    fill_rate = filled_qty / target_qty * 100.0 if target_qty > 0 else 0.0

    # Average fill price from execution history
    avg_fill: int | None = None
    if execution_history:
        total_value = 0
        total_qty = 0
        for fill in execution_history:
            price = fill.get("fill_price")
            qty = fill.get("quantity", 0)
            if price is not None and qty > 0:
                total_value += int(price) * int(qty)
                total_qty += int(qty)
        if total_qty > 0:
            avg_fill = total_value // total_qty

    # Arrival price: mid-price from the agent's known_bids/known_asks at first order
    # (POVExecutionAgent stores last_bid/last_ask; use first fill entry's context)
    arrival: int | None = None
    last_bid = getattr(agent, "last_bid", None)
    last_ask = getattr(agent, "last_ask", None)
    if last_bid is not None and last_ask is not None:
        arrival = (int(last_bid) + int(last_ask)) // 2

    # Session VWAP and total volume from liquidity metrics
    # Execution agents trade a single symbol
    symbol: str | None = getattr(agent, "symbol", None)
    session_vwap: int | None = None
    total_volume: int = 0
    if symbol is not None and symbol in symbol_liquidity:
        liq = symbol_liquidity[symbol]
        session_vwap = liq.vwap_cents
        total_volume = liq.total_exchanged_volume

    # Derived metrics
    vwap_slippage: int | None = None
    if avg_fill is not None and session_vwap is not None and session_vwap > 0:
        vwap_slippage = (avg_fill - session_vwap) * 10_000 // session_vwap

    participation: float | None = None
    if filled_qty > 0 and total_volume > 0:
        participation = filled_qty / total_volume * 100.0

    impl_shortfall: int | None = None
    if avg_fill is not None and arrival is not None and arrival > 0:
        impl_shortfall = (avg_fill - arrival) * 10_000 // arrival

    return ExecutionMetrics(
        target_quantity=target_qty,
        filled_quantity=filled_qty,
        fill_rate_pct=fill_rate,
        avg_fill_price_cents=avg_fill,
        vwap_cents=session_vwap,
        vwap_slippage_bps=vwap_slippage,
        participation_rate_pct=participation,
        arrival_price_cents=arrival,
        implementation_shortfall_bps=impl_shortfall,
    )

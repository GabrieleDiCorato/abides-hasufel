"""Pydantic config models for each built-in agent type.

Each model declares the agent-specific parameters with sensible defaults
(matching rmsc04 where applicable) and a ``create_agents()`` factory method
that instantiates actual ABIDES agent objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

import numpy as np
from pydantic import BaseModel, Field

from abides_core import NanosecondTime
from abides_core.utils import get_wake_time, str_to_ns


# ---------------------------------------------------------------------------
# Shared context passed to create_agents by the compiler
# ---------------------------------------------------------------------------
@dataclass
class AgentCreationContext:
    """Shared state from the compiler passed to every agent factory."""

    ticker: str
    mkt_open: NanosecondTime
    mkt_close: NanosecondTime
    log_orders: bool
    oracle_r_bar: int  # for derived params like SIGMA_N


# ---------------------------------------------------------------------------
# Base config
# ---------------------------------------------------------------------------
class BaseAgentConfig(BaseModel):
    """Common fields shared by all trading-agent configs."""

    model_config = {"extra": "forbid"}

    starting_cash: int = Field(
        default=10_000_000,
        description="Initial cash in cents ($100k = 10_000_000).",
    )
    log_orders: Optional[bool] = Field(
        default=None,
        description="Override per-agent order logging. None = use simulation-level setting.",
    )


# ---------------------------------------------------------------------------
# Noise Agent
# ---------------------------------------------------------------------------
class NoiseAgentConfig(BaseAgentConfig):
    """Configuration for NoiseAgent — simple agents that wake once and place a random order."""

    noise_mkt_open_offset: str = Field(
        default="-00:30:00",
        description="Offset from market open for noise wakeup window start (e.g. '-00:30:00').",
    )
    noise_mkt_close_time: str = Field(
        default="16:00:00",
        description="Time-of-day for noise wakeup window end.",
    )

    def create_agents(
        self,
        count: int,
        id_start: int,
        master_rng: np.random.RandomState,
        context: AgentCreationContext,
    ) -> List:
        from abides_markets.agents import NoiseAgent
        from abides_markets.models import OrderSizeModel

        log = self.log_orders if self.log_orders is not None else context.log_orders
        order_size_model = OrderSizeModel()

        noise_mkt_open = context.mkt_open + str_to_ns(self.noise_mkt_open_offset)
        # Parse close time relative to the date (mkt_open contains the date component)
        date_ns = context.mkt_open - str_to_ns("09:30:00")
        noise_mkt_close = date_ns + str_to_ns(self.noise_mkt_close_time)

        agents = []
        for j in range(id_start, id_start + count):
            agent_rng = np.random.RandomState(
                seed=master_rng.randint(low=0, high=2**32, dtype="uint64")
            )
            agents.append(
                NoiseAgent(
                    id=j,
                    name=f"NoiseAgent {j}",
                    type="NoiseAgent",
                    symbol=context.ticker,
                    starting_cash=self.starting_cash,
                    wakeup_time=get_wake_time(noise_mkt_open, noise_mkt_close, agent_rng),
                    log_orders=log,
                    order_size_model=order_size_model,
                    random_state=agent_rng,
                )
            )
        return agents


# ---------------------------------------------------------------------------
# Value Agent
# ---------------------------------------------------------------------------
class ValueAgentConfig(BaseAgentConfig):
    """Configuration for ValueAgent — Bayesian learner that estimates fundamental value."""

    r_bar: int = Field(
        default=100_000,
        description="True mean fundamental value in cents.",
    )
    kappa: float = Field(
        default=1.67e-15,
        description="Mean-reversion coefficient for agent's appraisal.",
    )
    lambda_a: float = Field(
        default=5.7e-12,
        description="Arrival rate (per nanosecond) for Poisson wakeups.",
    )
    sigma_n: Optional[int] = Field(
        default=None,
        description="Observation noise variance. Defaults to r_bar / 100.",
    )

    def create_agents(
        self,
        count: int,
        id_start: int,
        master_rng: np.random.RandomState,
        context: AgentCreationContext,
    ) -> List:
        from abides_markets.agents import ValueAgent
        from abides_markets.models import OrderSizeModel

        log = self.log_orders if self.log_orders is not None else context.log_orders
        order_size_model = OrderSizeModel()
        sigma_n = self.sigma_n if self.sigma_n is not None else self.r_bar / 100

        agents = [
            ValueAgent(
                id=j,
                name=f"Value Agent {j}",
                type="ValueAgent",
                symbol=context.ticker,
                starting_cash=self.starting_cash,
                sigma_n=sigma_n,
                r_bar=self.r_bar,
                kappa=self.kappa,
                lambda_a=self.lambda_a,
                log_orders=log,
                order_size_model=order_size_model,
                random_state=np.random.RandomState(
                    seed=master_rng.randint(low=0, high=2**32, dtype="uint64")
                ),
            )
            for j in range(id_start, id_start + count)
        ]
        return agents


# ---------------------------------------------------------------------------
# Momentum Agent
# ---------------------------------------------------------------------------
class MomentumAgentConfig(BaseAgentConfig):
    """Configuration for MomentumAgent — trend-follower using moving average crossover."""

    min_size: int = Field(default=1, description="Minimum order size.")
    max_size: int = Field(default=10, description="Maximum order size.")
    wake_up_freq: str = Field(
        default="37s",
        description="Wake-up frequency as duration string (e.g. '37s', '1min').",
    )
    poisson_arrival: bool = Field(
        default=True,
        description="If True, wakeup intervals are Poisson-distributed.",
    )

    def create_agents(
        self,
        count: int,
        id_start: int,
        master_rng: np.random.RandomState,
        context: AgentCreationContext,
    ) -> List:
        from abides_markets.agents import MomentumAgent
        from abides_markets.models import OrderSizeModel

        log = self.log_orders if self.log_orders is not None else context.log_orders
        order_size_model = OrderSizeModel()
        freq_ns = str_to_ns(self.wake_up_freq)

        agents = [
            MomentumAgent(
                id=j,
                name=f"MOMENTUM_AGENT_{j}",
                type="MomentumAgent",
                symbol=context.ticker,
                starting_cash=self.starting_cash,
                min_size=self.min_size,
                max_size=self.max_size,
                wake_up_freq=freq_ns,
                poisson_arrival=self.poisson_arrival,
                log_orders=log,
                order_size_model=order_size_model,
                random_state=np.random.RandomState(
                    seed=master_rng.randint(low=0, high=2**32, dtype="uint64")
                ),
            )
            for j in range(id_start, id_start + count)
        ]
        return agents


# ---------------------------------------------------------------------------
# Adaptive Market Maker Agent
# ---------------------------------------------------------------------------
class AdaptiveMarketMakerConfig(BaseAgentConfig):
    """Configuration for AdaptiveMarketMakerAgent — inventory-skewed ladder market maker."""

    pov: float = Field(default=0.025, description="Percentage of volume per level.")
    min_order_size: int = Field(default=1, description="Minimum order size at any level.")
    window_size: Union[int, str] = Field(
        default="adaptive",
        description="Spread window in ticks or 'adaptive'.",
    )
    num_ticks: int = Field(default=10, description="Number of price levels each side.")
    wake_up_freq: str = Field(
        default="60s",
        description="Wake-up frequency as duration string.",
    )
    poisson_arrival: bool = Field(default=True, description="Poisson-distributed wakeups.")
    cancel_limit_delay: int = Field(
        default=50,
        description="Delay in nanoseconds before cancel takes effect.",
    )
    skew_beta: float = Field(default=0, description="Inventory skew parameter.")
    price_skew_param: Optional[int] = Field(default=4, description="Price skew parameter.")
    level_spacing: float = Field(
        default=5,
        description="Spacing between price levels as fraction of spread.",
    )
    spread_alpha: float = Field(
        default=0.75,
        description="EWMA parameter for spread estimation.",
    )
    backstop_quantity: int = Field(default=0, description="Orders at the outermost level.")

    def create_agents(
        self,
        count: int,
        id_start: int,
        master_rng: np.random.RandomState,
        context: AgentCreationContext,
    ) -> List:
        from abides_markets.agents import AdaptiveMarketMakerAgent

        log = self.log_orders if self.log_orders is not None else context.log_orders
        freq_ns = str_to_ns(self.wake_up_freq)

        agents = [
            AdaptiveMarketMakerAgent(
                id=j,
                name=f"ADAPTIVE_POV_MARKET_MAKER_AGENT_{j}",
                type="AdaptivePOVMarketMakerAgent",
                symbol=context.ticker,
                starting_cash=self.starting_cash,
                pov=self.pov,
                min_order_size=self.min_order_size,
                window_size=self.window_size,
                num_ticks=self.num_ticks,
                wake_up_freq=freq_ns,
                poisson_arrival=self.poisson_arrival,
                cancel_limit_delay=self.cancel_limit_delay,
                skew_beta=self.skew_beta,
                price_skew_param=self.price_skew_param,
                level_spacing=self.level_spacing,
                spread_alpha=self.spread_alpha,
                backstop_quantity=self.backstop_quantity,
                log_orders=log,
                random_state=np.random.RandomState(
                    seed=master_rng.randint(low=0, high=2**32, dtype="uint64")
                ),
            )
            for j in range(id_start, id_start + count)
        ]
        return agents


# ---------------------------------------------------------------------------
# POV Execution Agent
# ---------------------------------------------------------------------------
class POVExecutionAgentConfig(BaseAgentConfig):
    """Configuration for POVExecutionAgent — executes large orders as percentage of volume."""

    start_time_offset: str = Field(
        default="00:30:00",
        description="Offset from market open when execution begins.",
    )
    end_time_offset: str = Field(
        default="00:30:00",
        description="Offset before market close when execution ends.",
    )
    freq: str = Field(default="1min", description="Wake-up frequency.")
    pov: float = Field(default=0.1, description="Target % of observed volume.")
    direction: str = Field(
        default="BID",
        description="Order direction: 'BID' (buy) or 'ASK' (sell).",
    )
    quantity: int = Field(default=1_200_000, description="Total target quantity.")
    trade: bool = Field(default=True, description="If False, only logs without trading.")

    def create_agents(
        self,
        count: int,
        id_start: int,
        master_rng: np.random.RandomState,
        context: AgentCreationContext,
    ) -> List:
        from abides_markets.agents import POVExecutionAgent
        from abides_markets.orders import Side

        log = self.log_orders if self.log_orders is not None else context.log_orders
        freq_ns = str_to_ns(self.freq)
        direction = Side.BID if self.direction.upper() == "BID" else Side.ASK
        exec_start = context.mkt_open + str_to_ns(self.start_time_offset)
        exec_end = context.mkt_close - str_to_ns(self.end_time_offset)

        agents = [
            POVExecutionAgent(
                id=j,
                name=f"POV_EXECUTION_AGENT_{j}",
                type="ExecutionAgent",
                symbol=context.ticker,
                starting_cash=self.starting_cash,
                start_time=exec_start,
                end_time=exec_end,
                freq=freq_ns,
                lookback_period=freq_ns,
                pov=self.pov,
                direction=direction,
                quantity=self.quantity,
                trade=self.trade,
                log_orders=log,
                random_state=np.random.RandomState(
                    seed=master_rng.randint(low=0, high=2**32, dtype="uint64")
                ),
            )
            for j in range(id_start, id_start + count)
        ]
        return agents

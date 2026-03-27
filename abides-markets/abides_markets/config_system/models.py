"""Pydantic models for the declarative simulation configuration.

Four clearly separated sections:
- **MarketConfig**: ticker, date, trading hours, oracle, exchange
- **AgentGroupConfig**: per-agent-type enable/disable, count, parameters
- **InfrastructureConfig**: latency model, computation delays
- **SimulationMeta**: seed, logging

These models serialize to/from YAML and JSON, and the compiler converts
a ``SimulationConfig`` into the runtime dict that ``Kernel`` expects.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Oracle configs (discriminated union via "type" field)
# ---------------------------------------------------------------------------
class SparseMeanRevertingOracleConfig(BaseModel):
    """Oracle using OU process with Poisson megashocks."""

    type: Literal["sparse_mean_reverting"] = "sparse_mean_reverting"
    r_bar: int = Field(default=100_000, description="Mean fundamental value in cents.")
    kappa: float = Field(
        default=1.67e-16, description="Mean-reversion speed of OU process."
    )
    sigma_s: float = Field(default=0, description="Shock variance.")
    fund_vol: float = Field(
        default=5e-5, description="Volatility (std) of the fundamental."
    )
    megashock_lambda_a: float = Field(
        default=2.77778e-18,
        description="Megashock arrival rate (per nanosecond).",
    )
    megashock_mean: float = Field(default=1000, description="Megashock mean.")
    megashock_var: float = Field(
        default=50_000, description="Megashock magnitude variance."
    )


class MeanRevertingOracleConfig(BaseModel):
    """Oracle using simple discrete mean-reversion process."""

    type: Literal["mean_reverting"] = "mean_reverting"
    r_bar: int = Field(default=100_000, description="Mean fundamental value in cents.")
    kappa: float = Field(default=0.05, description="Mean-reversion speed.")
    sigma_s: float = Field(default=100_000, description="Shock variance.")


class ExternalDataOracleConfig(BaseModel):
    """Oracle backed by external data (historical, CGAN, etc.).

    This is a marker config type signalling that the oracle will be injected
    at runtime via ``SimulationBuilder.oracle_instance()``.  The framework
    does not perform file I/O — the user is responsible for constructing an
    ``ExternalDataOracle`` with their chosen ``BatchDataProvider`` or
    ``PointDataProvider`` and passing it to the builder.
    """

    type: Literal["external_data"] = "external_data"


OracleConfig = Union[
    SparseMeanRevertingOracleConfig,
    MeanRevertingOracleConfig,
    ExternalDataOracleConfig,
]


# ---------------------------------------------------------------------------
# Exchange config (always exactly one exchange, nested in MarketConfig)
# ---------------------------------------------------------------------------
class ExchangeConfig(BaseModel):
    """Configuration for the ExchangeAgent (always agent id=0)."""

    book_logging: bool = Field(default=True, description="Log order book snapshots.")
    book_log_depth: int = Field(default=10, description="Depth of book snapshots.")
    stream_history_length: int = Field(
        default=500,
        description="Number of past orders stored for transacted volume computation.",
    )
    log_orders: bool = Field(
        default=False, description="Log all exchange order activity."
    )
    pipeline_delay: int = Field(
        default=0, description="Order acceptance latency in ns."
    )
    computation_delay: int = Field(
        default=0, description="Exchange computation delay in ns."
    )


# ---------------------------------------------------------------------------
# Market config (top-level section 1)
# ---------------------------------------------------------------------------
class MarketConfig(BaseModel):
    """General market parameters: ticker, trading hours, oracle, exchange."""

    ticker: str = Field(default="ABM", description="Trading symbol.")
    date: str = Field(default="20210205", description="Simulation date (YYYYMMDD).")
    start_time: str = Field(default="09:30:00", description="Market open time.")
    end_time: str = Field(default="10:00:00", description="Market close time.")
    oracle: OracleConfig | None = Field(
        description=(
            "Oracle configuration.  Set to an OracleConfig to enable a "
            "fundamental-value oracle (required for ValueAgent).  Set to "
            "None for oracle-less simulations using only LOB-based agents "
            "(Noise, Momentum, AMM, POV).  There is no default — this "
            "field must be set explicitly."
        ),
    )
    opening_price: int | None = Field(
        default=None,
        description=(
            "Opening price in integer cents (e.g. $100.00 = 10_000).  "
            "Required when oracle is None — provides the ExchangeAgent's "
            "seed price.  Ignored when an oracle is present (the oracle "
            "provides opening prices via get_daily_open_price())."
        ),
    )
    exchange: ExchangeConfig = Field(
        default_factory=ExchangeConfig,
        description="Exchange agent configuration.",
    )


# ---------------------------------------------------------------------------
# Agent group config (top-level section 2)
# ---------------------------------------------------------------------------
class AgentGroupConfig(BaseModel):
    """Configuration for a group of agents of the same type."""

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=True, description="Whether this agent group is active."
    )
    count: int = Field(ge=0, description="Number of agents of this type.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Agent-specific parameters "
            "(validated against registry schema at compile time)."
        ),
    )


# ---------------------------------------------------------------------------
# Infrastructure config (top-level section 3)
# ---------------------------------------------------------------------------
class LatencyConfig(BaseModel):
    """Network latency model configuration."""

    type: str = Field(
        default="deterministic",
        description="Latency type: 'deterministic' or 'no_latency'.",
    )


class InfrastructureConfig(BaseModel):
    """Physical infrastructure: network latency and computation delays."""

    latency: LatencyConfig = Field(
        default_factory=LatencyConfig,
        description="Network latency model.",
    )
    default_computation_delay: int = Field(
        default=50,
        description="Default computation delay per agent action in nanoseconds.",
    )


# ---------------------------------------------------------------------------
# Simulation meta (top-level section 4)
# ---------------------------------------------------------------------------
class SimulationMeta(BaseModel):
    """Simulation-level parameters: seed, logging."""

    seed: Union[int, Literal["random"]] = Field(
        default="random",
        description="RNG seed for reproducibility. Use 'random' for a fresh seed.",
    )
    log_level: str = Field(default="INFO", description="Stdout log level.")
    log_orders: bool = Field(
        default=True, description="Enable order logging for all agents."
    )


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------
class SimulationConfig(BaseModel):
    """Top-level simulation configuration.

    Four clearly separated sections:
    - ``market``: ticker, date, trading hours, oracle, exchange
    - ``agents``: dict mapping agent type name → AgentGroupConfig
    - ``infrastructure``: latency, computation delay
    - ``simulation``: seed, logging
    """

    market: MarketConfig = Field(
        default_factory=MarketConfig,
        description="Market parameters.",
    )
    agents: dict[str, AgentGroupConfig] = Field(
        default_factory=dict,
        description="Agent groups keyed by registered agent type name.",
    )
    infrastructure: InfrastructureConfig = Field(
        default_factory=InfrastructureConfig,
        description="Infrastructure parameters.",
    )
    simulation: SimulationMeta = Field(
        default_factory=SimulationMeta,
        description="Simulation-level parameters.",
    )

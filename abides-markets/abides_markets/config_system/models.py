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

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Oracle configs (discriminated union via "type" field)
# ---------------------------------------------------------------------------
class SparseMeanRevertingOracleConfig(BaseModel):
    """Oracle using OU process with Poisson megashocks."""

    type: Literal["sparse_mean_reverting"] = "sparse_mean_reverting"
    r_bar: int = Field(
        default=100_000,
        description=(
            "Mean fundamental value in integer cents (e.g. $100.00 = 10_000). "
            "This is the long-run price the OU process reverts toward."
        ),
        examples=[100_000, 50_000],
        json_schema_extra={"unit": "cents"},
    )
    mean_reversion_half_life: str = Field(
        default="48d",
        description=(
            "Half-life of mean reversion as a duration string "
            "(e.g. '48d', '1152h'). After this much time the "
            "fundamental price has reverted halfway to r_bar. "
            "Converted to the OU kappa via ln(2)/half_life_ns."
        ),
        examples=["48d", "30d", "7d"],
        json_schema_extra={"format": "duration"},
    )
    sigma_s: float = Field(
        default=0,
        description=(
            "Variance of per-step shocks to the fundamental price. "
            "Higher values produce more volatile fundamental dynamics."
        ),
    )
    fund_vol: float = Field(
        default=5e-5,
        description=(
            "Per-sqrt(nanosecond) volatility of the OU diffusion term. "
            "Controls the magnitude of continuous random fluctuations "
            "around r_bar. The default (5e-5) produces roughly ±$4.60 "
            "daily variation on a $1000 stock."
        ),
    )
    megashock_mean_interval: str | None = Field(
        default="100000h",
        description=(
            "Average time between megashock events as a duration string "
            "(e.g. '100000h' ≈ 11.4 years). Megashocks are rare, large "
            "jumps in fundamental value. Set to None to disable "
            "megashocks entirely."
        ),
        examples=["100000h", "50000h", "10000h"],
        json_schema_extra={"format": "duration"},
    )
    kappa: float | None = Field(
        default=None,
        description=(
            "Per-nanosecond OU mean-reversion rate (alternative to "
            "mean_reversion_half_life).  When set, mean_reversion_half_life "
            "must not be explicitly provided — the two are mutually exclusive."
        ),
    )
    megashock_lambda_a: float | None = Field(
        default=None,
        description=(
            "Per-nanosecond Poisson megashock arrival rate (alternative to "
            "megashock_mean_interval).  When set, megashock_mean_interval "
            "must not be explicitly provided — the two are mutually exclusive."
        ),
    )
    megashock_mean: float = Field(
        default=1000,
        description="Mean magnitude of megashock jumps (in cents).",
        json_schema_extra={"unit": "cents"},
    )
    megashock_var: float = Field(
        default=50_000,
        description="Variance of megashock magnitude distribution.",
    )

    @model_validator(mode="after")
    def _validate_no_dual_specification(self) -> SparseMeanRevertingOracleConfig:
        """Reject configs that specify both the raw rate and the duration string."""
        if (
            self.kappa is not None
            and "mean_reversion_half_life" in self.model_fields_set
        ):
            raise ValueError(
                "Cannot set both 'kappa' and 'mean_reversion_half_life' — "
                "they are mutually exclusive representations of the same "
                "mean-reversion speed.  Use one or the other."
            )
        if (
            self.megashock_lambda_a is not None
            and "megashock_mean_interval" in self.model_fields_set
        ):
            raise ValueError(
                "Cannot set both 'megashock_lambda_a' and "
                "'megashock_mean_interval' — they are mutually exclusive "
                "representations of the same megashock frequency.  "
                "Use one or the other."
            )
        return self


class MeanRevertingOracleConfig(BaseModel):
    """Oracle using simple discrete mean-reversion process.

    .. deprecated::
        Use ``SparseMeanRevertingOracleConfig`` instead.  This oracle steps
        once per nanosecond, which is prohibitively expensive for long
        simulations (> 1 M steps raises ``ValueError``).
    """

    type: Literal["mean_reverting"] = "mean_reverting"
    r_bar: int = Field(
        default=100_000,
        description="Mean fundamental value in integer cents (e.g. $100.00 = 10_000).",
        examples=[100_000, 50_000],
        json_schema_extra={"unit": "cents"},
    )
    kappa: float = Field(
        default=0.05,
        description=(
            "Per-nanosecond mean-reversion coefficient (dimensionless).  "
            "Since this oracle steps once per nanosecond, kappa is the "
            "fraction of the gap between the current fundamental and r_bar "
            "that closes each nanosecond.  Larger values produce faster "
            "reversion.  Prefer SparseMeanRevertingOracleConfig (which "
            "accepts a human-readable half-life duration) for new work."
        ),
    )
    sigma_s: float = Field(
        default=100_000,
        description="Variance of per-step shocks to the fundamental price.",
    )


class ExternalDataOracleConfig(BaseModel):
    """Oracle backed by external data (historical, CGAN, etc.).

    This is a marker config type signalling that the oracle will be injected
    at runtime via ``SimulationBuilder.oracle_instance()``.  The framework
    does not perform file I/O — the user is responsible for constructing an
    ``ExternalDataOracle`` with their chosen ``BatchDataProvider`` or
    ``PointDataProvider`` and passing it to the builder.
    """

    type: Literal["external_data"] = "external_data"


OracleConfig = (
    SparseMeanRevertingOracleConfig
    | MeanRevertingOracleConfig
    | ExternalDataOracleConfig
)


# ---------------------------------------------------------------------------
# Exchange config (always exactly one exchange, nested in MarketConfig)
# ---------------------------------------------------------------------------
class ExchangeConfig(BaseModel):
    """Configuration for the ExchangeAgent (always agent id=0)."""

    book_logging: bool = Field(
        default=True,
        description=(
            "Log order book snapshots at each book update. Required for "
            "L1/L2 time-series extraction in SimulationResult."
        ),
    )
    book_log_depth: int = Field(
        default=10,
        ge=1,
        description=(
            "Number of price levels to capture per side in book snapshots. "
            "Higher values capture deeper book state but use more memory."
        ),
    )
    book_capture: Literal["off", "l1", "l2"] | None = Field(
        default=None,
        description=(
            "Order-book capture mode.  'off' disables capture entirely.  "
            "'l1' only publishes a snapshot when the top-of-book price/size "
            "changes (cheapest; sufficient for L1 consumers).  'l2' "
            "publishes the full depth-N L2 snapshot on every book mutation "
            "(byte-equivalent to legacy book_logging=True).  When None "
            "(default), falls back to the legacy 'book_logging' flag: "
            "True -> 'l2', False -> 'off'."
        ),
    )
    stream_history_length: int = Field(
        default=500,
        ge=1,
        description=(
            "Number of recent trades stored for transacted volume computation. "
            "Used by market-maker agents (AdaptiveMarketMaker) to estimate "
            "participation-of-volume. Higher values give smoother volume "
            "estimates but increase memory usage."
        ),
    )
    log_orders: bool = Field(
        default=False,
        description=(
            "Log all exchange order activity (submits, cancels, fills). "
            "Enables detailed post-simulation order-flow analysis but "
            "significantly increases log size."
        ),
    )
    pipeline_delay: int = Field(
        default=0,
        ge=0,
        description=(
            "Order acceptance latency in nanoseconds. Simulates the delay "
            "between an order reaching the exchange and being processed. "
            "0 = immediate processing."
        ),
        json_schema_extra={"unit": "nanoseconds"},
    )
    computation_delay: int = Field(
        default=0,
        ge=0,
        description=(
            "Exchange computation delay in nanoseconds. Added to every "
            "exchange action (match, cancel, etc.). 0 = no extra delay."
        ),
        json_schema_extra={"unit": "nanoseconds"},
    )


# ---------------------------------------------------------------------------
# Market config (top-level section 1)
# ---------------------------------------------------------------------------
class MarketConfig(BaseModel):
    """General market parameters: ticker, trading hours, oracle, exchange."""

    ticker: str = Field(
        default="ABM", description="Trading symbol.", examples=["ABM", "AAPL"]
    )
    date: str = Field(
        default="20210205",
        description="Simulation date in YYYYMMDD format.",
        pattern=r"^\d{8}$",
        examples=["20210205", "20230915"],
    )
    start_time: str = Field(
        default="09:30:00",
        description="Market open time in HH:MM:SS format.",
        pattern=r"^\d{2}:\d{2}:\d{2}$",
        examples=["09:30:00", "08:00:00"],
    )
    end_time: str = Field(
        default="10:00:00",
        description="Market close time in HH:MM:SS format.",
        pattern=r"^\d{2}:\d{2}:\d{2}$",
        examples=["10:00:00", "16:00:00"],
    )
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
        examples=[10_000, 100_000],
        json_schema_extra={"unit": "cents"},
    )

    @field_validator("date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{8}", v):
            raise ValueError(f"date must be in YYYYMMDD format, got '{v}'")
        month, day = int(v[4:6]), int(v[6:8])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError(f"date has invalid month/day: month={month}, day={day}")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", v):
            raise ValueError(f"Time must be in HH:MM:SS format, got '{v}'")
        parts = v.split(":")
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
            raise ValueError(f"Time has invalid components: {h:02d}:{m:02d}:{s:02d}")
        return v

    @model_validator(mode="after")
    def _validate_oracle_opening_price(self) -> MarketConfig:
        """Ensure oracle-less configs provide an opening price."""
        if self.oracle is None and self.opening_price is None:
            raise ValueError(
                "opening_price is required when oracle is None — the "
                "ExchangeAgent needs a seed price."
            )
        return self

    @model_validator(mode="after")
    def _validate_time_ordering(self) -> MarketConfig:
        """Reject start_time >= end_time (inverted or zero-length market)."""
        if self.start_time >= self.end_time:
            raise ValueError(
                f"start_time ({self.start_time}) must be before "
                f"end_time ({self.end_time})."
            )
        return self

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

    type: Literal["deterministic", "no_latency"] = Field(
        default="deterministic",
        description=(
            "Latency model type. 'deterministic' adds realistic network "
            "delays between agents and the exchange. 'no_latency' removes "
            "all network delays (useful for unit testing)."
        ),
    )


class InfrastructureConfig(BaseModel):
    """Physical infrastructure: network latency and computation delays."""

    latency: LatencyConfig = Field(
        default_factory=LatencyConfig,
        description="Network latency model.",
    )
    default_computation_delay: int = Field(
        default=50,
        ge=0,
        description=(
            "Default computation delay per agent action in nanoseconds. "
            "Simulates the time an agent spends processing before acting. "
            "Can be overridden per agent type via agent config."
        ),
        json_schema_extra={"unit": "nanoseconds"},
    )
    computation_delay_by_name: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Per-agent-name overrides for computation delay (in nanoseconds). "
            "Resolved by the compiler against ``Agent.name`` after agents are "
            "instantiated; unmatched names raise an error.  Highest precedence: "
            "wins over both ``default_computation_delay`` and the per-type "
            "override on the agent group config."
        ),
    )


# ---------------------------------------------------------------------------
# Event sink configs (discriminated union via "kind" field)
# ---------------------------------------------------------------------------
class _SinkConfigBase(BaseModel):
    """Base for all sink configs: strict (no extras)."""

    model_config = {"extra": "forbid"}


class MemorySinkConfig(_SinkConfigBase):
    """In-memory columnar event sink (``InMemorySink``).

    Captures every published event into parallel column arrays so the
    runner can materialise ``SimulationResult.logs`` without re-parsing
    a pickle stream.  Cheap; always-on in the default sink set.
    """

    kind: Literal["memory"] = Field(
        default="memory",
        description="Discriminator selecting the in-memory columnar sink.",
    )


class BZ2PickleSinkConfig(_SinkConfigBase):
    """Legacy bz2-pickled per-agent log stream (``BZ2PickleSink``).

    At simulation end, writes one ``<AgentName>.bz2`` file per agent
    that produced events under ``<log_root>/<run_id>/`` (plus an
    optional ``summary_log.bz2`` when the kernel's ``skip_log`` is
    ``False``).  Each file is the bz2-compressed pickle of a
    ``DataFrame`` with columns ``[EventType, Event]`` indexed by
    ``EventTime``, matching the pre-EventBus layout.  Retained for
    backward compatibility; targeted for removal in Phase 5.  The
    output path is derived from ``SimulationMeta.log_root``; this
    config takes no per-sink fields.
    """

    kind: Literal["bz2_pickle"] = Field(
        default="bz2_pickle",
        description=(
            "Discriminator selecting the legacy bz2-pickled " "per-agent log sink."
        ),
    )


class ParquetSinkConfig(_SinkConfigBase):
    """Streaming Parquet event sink (``ParquetSink``).

    Spills event rows to one or more Parquet files under
    ``<log_root>/<run_id>/``.  Requires the optional ``pyarrow``
    dependency (``pip install abides-ng[parquet]``).  The three
    ``accept_*`` flags map 1:1 onto the same-named coarse filters on
    ``EventSink`` and let you skip whole event categories at the sink
    boundary.
    """

    kind: Literal["parquet"] = Field(
        default="parquet",
        description="Discriminator selecting the streaming Parquet sink.",
    )
    compression: Literal["snappy", "zstd", "none"] = Field(
        default="zstd",
        description="Parquet column compression codec.",
    )
    checkpoint_every_rows: int | None = Field(
        default=None,
        description=(
            "If set, rotate each per-key bucket to a new Parquet file "
            "after this many rows.  ``None`` (default) writes one file "
            "per key at simulation end.  Must be positive when set."
        ),
    )
    accept_events: bool = Field(
        default=True,
        description="If False, skip business-event capture entirely.",
    )
    accept_metrics: bool = Field(
        default=True,
        description="If False, skip metric capture entirely.",
    )
    accept_book_snapshots: bool = Field(
        default=True,
        description="If False, skip order-book snapshot capture entirely.",
    )

    @field_validator("checkpoint_every_rows")
    @classmethod
    def _validate_checkpoint(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(
                f"checkpoint_every_rows must be positive or None, got {v!r}"
            )
        return v


class OrderBookSnapshotMemorySinkConfig(_SinkConfigBase):
    """In-memory order-book snapshot sink (``OrderBookSnapshotMemorySink``).

    Records L1/L2 snapshots published by the order book.  Required for
    ``SimulationResult.l1_snapshots`` / ``.l2_snapshots`` extraction.
    """

    kind: Literal["orderbook_snapshot_memory"] = Field(
        default="orderbook_snapshot_memory",
        description=(
            "Discriminator selecting the in-memory per-symbol "
            "order-book snapshot sink."
        ),
    )
    symbol: str | None = Field(
        default=None,
        description=(
            "Symbol this sink captures.  When ``None`` (default), the "
            "compiler expands the config into one sink per exchange "
            "symbol.  When set, the symbol must exist on the "
            "ExchangeAgent or compilation fails with ``ConfigError``.  "
            "To capture more than one specific symbol, add one config "
            "per symbol to ``event_sinks``."
        ),
    )


class OrderBookHistoryMemorySinkConfig(_SinkConfigBase):
    """In-memory order-book event-history sink (``OrderBookHistoryMemorySink``).

    Records the full sequence of order-book mutation events
    (limits, executions, cancels, modifies, replaces) for trade
    reconstruction.  Required for ``SimulationResult.trades`` and
    ``.liquidity`` extraction.
    """

    kind: Literal["orderbook_history_memory"] = Field(
        default="orderbook_history_memory",
        description=(
            "Discriminator selecting the in-memory per-symbol "
            "order-book event-history sink."
        ),
    )
    symbol: str | None = Field(
        default=None,
        description=(
            "Symbol this sink captures.  When ``None`` (default), the "
            "compiler expands the config into one sink per exchange "
            "symbol.  When set, the symbol must exist on the "
            "ExchangeAgent or compilation fails with ``ConfigError``.  "
            "To capture more than one specific symbol, add one config "
            "per symbol to ``event_sinks``."
        ),
    )


SinkConfig = Annotated[
    MemorySinkConfig
    | BZ2PickleSinkConfig
    | ParquetSinkConfig
    | OrderBookSnapshotMemorySinkConfig
    | OrderBookHistoryMemorySinkConfig,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Simulation meta (top-level section 4)
# ---------------------------------------------------------------------------
class SimulationMeta(BaseModel):
    """Simulation-level parameters: seed, logging, event sinks."""

    seed: int | Literal["random"] = Field(
        default="random",
        description=(
            "RNG seed for reproducibility. Use an integer for deterministic "
            "runs or 'random' for a fresh seed each time."
        ),
        examples=[42, 12345, "random"],
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Stdout log level for kernel and agent messages.",
    )
    log_orders: bool = Field(
        default=True,
        description=(
            "Enable order logging for all agents. Individual agents can "
            "override this via their own log_orders setting."
        ),
    )
    log_root: str = Field(
        default="./log",
        description=(
            "Base directory for run-scoped log output.  Each simulation "
            "run writes under ``<log_root>/<run_id>/``.  Consumed by the "
            "``BZ2PickleSink`` and ``ParquetSink`` event sinks; ignored "
            "by purely in-memory sinks."
        ),
        examples=["./log", "/var/abides/log"],
    )
    event_sinks: list[SinkConfig] | None = Field(
        default=None,
        description=(
            "Explicit list of event sinks for this run.  When ``None`` "
            "(default), the compiler synthesises the backward-compatible "
            "default set: one ``memory`` sink, plus — for every exchange "
            "symbol — one ``orderbook_history_memory`` sink (always) and "
            "one ``orderbook_snapshot_memory`` sink when "
            "``market.exchange.book_capture`` is not ``'off'``.  Note "
            "that the ``bz2_pickle`` sink is *not* in the default set; "
            "the kernel attaches it independently from its own "
            "``LogWriter`` (so it appears in every run that uses the "
            "default kernel ``log_writer`` regardless of the list "
            "below).  When ``event_sinks`` is set explicitly, "
            "``market.exchange.book_logging`` must be ``False`` — "
            "combining the two raises ``ConfigError`` because their "
            "sink-registration intents conflict."
        ),
    )
    show_trace_messages: bool = Field(
        default=False,
        description=(
            "If True, the kernel logs every dispatched message at TRACE "
            "level.  Extremely verbose; intended for low-level debugging "
            "of message routing only."
        ),
    )
    disable_event_log_for: list[str] = Field(
        default_factory=list,
        description=(
            'Agent type names (registered names, e.g. ``"NoiseAgent"``) '
            "whose ``log_events`` flag is forced to ``False`` at build "
            "time.  Useful for silencing chatty agents without editing "
            "their per-group configs."
        ),
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

    Agent groups are sorted by name to guarantee deterministic seed
    assignment regardless of insertion order.
    """

    market: MarketConfig = Field(
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

    @model_validator(mode="after")
    def _sort_agents(self) -> SimulationConfig:
        """Sort agent groups by name for deterministic seed assignment."""
        self.agents = dict(sorted(self.agents.items()))
        return self

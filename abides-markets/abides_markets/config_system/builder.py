"""Fluent builder API for constructing SimulationConfig.

Usage::

    config = (SimulationBuilder()
        .apply_template("rmsc04")
        .ticker("AAPL").date("20210205")
        .enable_agent(NoiseAgentConfig(multi_wake=True), count=1000)
        .enable_agent(ValueAgentConfig(r_bar=100_000), count=102)
        .disable_agent("momentum")
        .latency(LatencyConfig(type="deterministic"))
        .seed(42)
        .build())
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, Literal

from abides_markets.config_system.models import (
    AgentGroupConfig,
    ExchangeConfig,
    InfrastructureConfig,
    LatencyConfig,
    MarketConfig,
    OracleConfig,
    SimulationConfig,
    SimulationMeta,
)
from abides_markets.config_system.templates import get_template

if TYPE_CHECKING:
    from abides_markets.config_system.agent_configs import BaseAgentConfig
    from abides_markets.config_system.models import SinkConfig


def _deep_merge_dicts(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*; overlay values win."""
    result = {**base}
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


class SimulationBuilder:
    """Fluent builder for SimulationConfig.

    Supports template stacking, per-agent overrides, and a final
    ``build()`` that validates and returns a ``SimulationConfig``.
    """

    def __init__(self) -> None:
        self._market: MarketConfig | None = None
        self._agents: dict[str, AgentGroupConfig] = {}
        self._infrastructure: InfrastructureConfig | None = None
        self._simulation: SimulationMeta | None = None

    # ------------------------------------------------------------------
    # Template API
    # ------------------------------------------------------------------

    def apply_template(self, name: str) -> SimulationBuilder:
        """Deep-merge a template into the current config.

        Multiple templates can be stacked — later ones override earlier ones.
        """
        template = get_template(name)
        self._apply_template_dict(template)
        return self

    def _apply_template_dict(self, d: dict[str, Any]) -> None:
        """Merge a raw template dict into typed builder state."""
        if "market" in d:
            mkt_data = d["market"]
            if self._market is None:
                self._market = MarketConfig.model_validate(mkt_data)
            else:
                merged = _deep_merge_dicts(self._market.model_dump(), mkt_data)
                self._market = MarketConfig.model_validate(merged)

        if "agents" in d:
            for name, agent_data in d["agents"].items():
                if name in self._agents:
                    existing = self._agents[name].model_dump()
                    merged_params = _deep_merge_dicts(
                        existing.get("params", {}), agent_data.get("params", {})
                    )
                    merged = _deep_merge_dicts(existing, agent_data)
                    merged["params"] = merged_params
                    self._agents[name] = AgentGroupConfig.model_validate(merged)
                else:
                    self._agents[name] = AgentGroupConfig.model_validate(agent_data)

        if "infrastructure" in d:
            infra_data = d["infrastructure"]
            if self._infrastructure is None:
                self._infrastructure = InfrastructureConfig.model_validate(infra_data)
            else:
                merged = _deep_merge_dicts(
                    self._infrastructure.model_dump(), infra_data
                )
                self._infrastructure = InfrastructureConfig.model_validate(merged)

        if "simulation" in d:
            sim_data = d["simulation"]
            if self._simulation is None:
                self._simulation = SimulationMeta.model_validate(sim_data)
            else:
                merged = _deep_merge_dicts(self._simulation.model_dump(), sim_data)
                self._simulation = SimulationMeta.model_validate(merged)

    # ------------------------------------------------------------------
    # Market field setters
    # ------------------------------------------------------------------

    def market(self, config: MarketConfig) -> SimulationBuilder:
        """Replace the entire market config."""
        self._market = config
        return self

    def ticker(self, value: str) -> SimulationBuilder:
        """Set the trading symbol."""
        if self._market is None:
            self._market = MarketConfig.model_construct(ticker=value)
        else:
            self._market = self._market.model_copy(update={"ticker": value})
        return self

    def date(self, value: str) -> SimulationBuilder:
        """Set the simulation date (YYYYMMDD)."""
        if self._market is None:
            self._market = MarketConfig.model_construct(date=value)
        else:
            self._market = self._market.model_copy(update={"date": value})
        return self

    def start_time(self, value: str) -> SimulationBuilder:
        """Set the market open time (HH:MM:SS)."""
        if self._market is None:
            self._market = MarketConfig.model_construct(start_time=value)
        else:
            self._market = self._market.model_copy(update={"start_time": value})
        return self

    def end_time(self, value: str) -> SimulationBuilder:
        """Set the market close time (HH:MM:SS)."""
        if self._market is None:
            self._market = MarketConfig.model_construct(end_time=value)
        else:
            self._market = self._market.model_copy(update={"end_time": value})
        return self

    def oracle(self, config: OracleConfig | None) -> SimulationBuilder:
        """Set the oracle config, or pass ``None`` to run oracle-less.

        When ``config`` is ``None``, call ``.opening_price()`` before
        ``.build()`` to provide a seed price for the exchange.
        """
        if self._market is None:
            if config is None:
                raise ValueError(
                    "Call .apply_template() or .market() before .oracle(None). "
                    "A market configuration is required to disable the oracle."
                )
            self._market = MarketConfig.model_construct(oracle=config)
        else:
            self._market = self._market.model_copy(update={"oracle": config})
        return self

    def opening_price(self, price: int) -> SimulationBuilder:
        """Set the exchange seed price in cents (required when oracle is None)."""
        if self._market is None:
            self._market = MarketConfig.model_construct(opening_price=price)
        else:
            self._market = self._market.model_copy(update={"opening_price": price})
        return self

    def exchange(self, config: ExchangeConfig) -> SimulationBuilder:
        """Replace the exchange config."""
        if self._market is None:
            raise ValueError("Call .apply_template() or .market() before .exchange().")
        self._market = self._market.model_copy(update={"exchange": config})
        return self

    # ------------------------------------------------------------------
    # Agent methods
    # ------------------------------------------------------------------

    def enable_agent(self, config: BaseAgentConfig, count: int) -> SimulationBuilder:
        """Enable an agent type with the given count.

        Args:
            config: Typed agent config instance (e.g. ``NoiseAgentConfig()``).
            count: Number of agents to create.
        """
        from abides_markets.config_system.registry import registry

        cls = type(config)
        name = registry.name_for_class(cls)
        if name is None:
            raise ValueError(
                f"Agent config class {cls.__name__!r} is not registered. "
                f"Use @register_agent to register it before calling enable_agent()."
            )
        self._agents[name] = AgentGroupConfig(
            enabled=True,
            count=count,
            params=config.model_dump(exclude_unset=True),
        )
        return self

    def disable_agent(self, agent: type | str) -> SimulationBuilder:
        """Disable an agent type.

        Args:
            agent: Either the registry name (string, e.g. ``"value"``) or
                   the config model class (e.g. ``ValueAgentConfig``).
        """
        if isinstance(agent, str):
            name: str = agent
        else:
            from abides_markets.config_system.registry import registry

            _name = registry.name_for_class(agent)
            if _name is None:
                raise ValueError(
                    f"Agent config class {agent.__name__!r} is not registered."
                )
            name = _name
        if name in self._agents:
            self._agents[name] = self._agents[name].model_copy(
                update={"enabled": False}
            )
        else:
            self._agents[name] = AgentGroupConfig(enabled=False, count=0, params={})
        return self

    def agent_computation_delay_by_type(
        self, agent_type: str, delay: int
    ) -> SimulationBuilder:
        """Set the computation delay for every agent of a registered type."""
        if agent_type in self._agents:
            existing_params = dict(self._agents[agent_type].params)
            existing_params["computation_delay"] = delay
            self._agents[agent_type] = self._agents[agent_type].model_copy(
                update={"params": existing_params}
            )
        else:
            self._agents[agent_type] = AgentGroupConfig(
                enabled=True, count=0, params={"computation_delay": delay}
            )
        return self

    def agent_computation_delay_by_name(
        self, agent_name: str, delay: int
    ) -> SimulationBuilder:
        """Override the computation delay of a single agent by its ``name``."""
        infra = self._infrastructure or InfrastructureConfig()
        overrides = dict(infra.computation_delay_by_name)
        overrides[agent_name] = delay
        self._infrastructure = infra.model_copy(
            update={"computation_delay_by_name": overrides}
        )
        return self

    def agent_computation_delay(self, name: str, delay: int) -> SimulationBuilder:
        """Alias for :meth:`agent_computation_delay_by_type`."""
        return self.agent_computation_delay_by_type(name, delay)

    # ------------------------------------------------------------------
    # Infrastructure setters
    # ------------------------------------------------------------------

    def latency(self, config: LatencyConfig) -> SimulationBuilder:
        """Set the latency model config."""
        infra = self._infrastructure or InfrastructureConfig()
        self._infrastructure = infra.model_copy(update={"latency": config})
        return self

    def computation_delay(self, delay: int) -> SimulationBuilder:
        """Set the default computation delay in nanoseconds."""
        infra = self._infrastructure or InfrastructureConfig()
        self._infrastructure = infra.model_copy(
            update={"default_computation_delay": delay}
        )
        return self

    def infrastructure(self, config: InfrastructureConfig) -> SimulationBuilder:
        """Replace the entire infrastructure config."""
        self._infrastructure = config
        return self

    # ------------------------------------------------------------------
    # Simulation meta setters
    # ------------------------------------------------------------------

    def seed(self, seed: int | Literal["random"]) -> SimulationBuilder:
        """Set the RNG seed. Use ``"random"`` for a fresh seed."""
        sim = self._simulation or SimulationMeta()
        self._simulation = sim.model_copy(update={"seed": seed})
        return self

    def log_level(self, level: str) -> SimulationBuilder:
        """Set stdout log level."""
        sim = self._simulation or SimulationMeta()
        self._simulation = sim.model_copy(update={"log_level": level})
        return self

    def log_orders(self, enabled: bool) -> SimulationBuilder:
        """Set global order logging."""
        sim = self._simulation or SimulationMeta()
        self._simulation = sim.model_copy(update={"log_orders": enabled})
        return self

    def meta(self, config: SimulationMeta) -> SimulationBuilder:
        """Replace the entire simulation meta config."""
        self._simulation = config
        return self

    def event_sinks(self, *sinks: SinkConfig) -> SimulationBuilder:
        """Set the ``event_sinks`` list on ``SimulationMeta``."""
        sim = self._simulation or SimulationMeta()
        self._simulation = sim.model_copy(update={"event_sinks": list(sinks)})
        return self

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> SimulationConfig:
        """Validate and return the SimulationConfig.

        Raises:
            ValueError: If the market config is not set, or semantic
                constraints are violated (ValueAgent without oracle, etc.).
            pydantic.ValidationError: If the assembled configuration is invalid.
        """
        from abides_markets.config_system.registry import registry

        if self._market is None:
            raise ValueError(
                "Market config is required. Call .apply_template() or .market() first."
            )

        # Re-validate market (catches cross-field constraints deferred by individual setters)
        validated_market = MarketConfig.model_validate(self._market.model_dump())

        config = SimulationConfig(
            market=validated_market,
            agents=self._agents,
            infrastructure=self._infrastructure or InfrastructureConfig(),
            simulation=self._simulation or SimulationMeta(),
        )

        # Eager validation: validate agent params against registry config models
        for agent_name, group in config.agents.items():
            if not group.enabled:
                continue
            try:
                entry = registry.get(agent_name)
            except KeyError as e:
                raise ValueError(
                    f"Agent type {agent_name!r} is not registered. "
                    f"Available types: {', '.join(registry.registered_names())}"
                ) from e
            try:
                entry.config_model(**group.params)
            except Exception as e:
                raise ValueError(
                    f"Invalid parameters for agent type {agent_name!r}: {e}"
                ) from e

        # Oracle-related validation
        oracle_present = config.market.oracle is not None
        for agent_name, group in config.agents.items():
            if not group.enabled or group.count == 0:
                continue
            if agent_name == "value" and not oracle_present:
                raise ValueError(
                    "ValueAgent requires an oracle for fundamental-value observations, "
                    "but no oracle is configured. Either set market.oracle or remove "
                    "ValueAgent from the simulation."
                )
        if not oracle_present and config.market.opening_price is None:
            raise ValueError(
                "When no oracle is configured, market.opening_price must be set "
                "to provide the ExchangeAgent with a seed price "
                "(integer cents, e.g. 10_000 = $100.00)."
            )

        self._cross_validate(config)
        return config

    @classmethod
    def from_config(cls, config: SimulationConfig) -> SimulationBuilder:
        """Construct a builder pre-loaded from an existing SimulationConfig.

        Enables round-tripping: ``config.to_builder().seed(99).build()``.
        """
        builder = cls()
        builder._market = config.market
        builder._agents = dict(config.agents)
        builder._infrastructure = config.infrastructure
        builder._simulation = config.simulation
        return builder

    def to_dict(self) -> dict[str, Any]:
        """Build and return a plain dict (via model_dump) for inspection.

        Equivalent to ``builder.build().model_dump()``.
        """
        result: dict[str, Any] = self.build().model_dump()
        return result

    def build_and_compile(self) -> dict[str, Any]:
        """Build, validate, and compile in one step."""
        from abides_markets.config_system.compiler import compile as compile_config

        config = self.build()
        return compile_config(config)

    # ------------------------------------------------------------------
    # Cross-agent / cross-section consistency checks
    # ------------------------------------------------------------------

    @staticmethod
    def _cross_validate(config: SimulationConfig) -> None:
        """Emit warnings for semantically suspect but technically valid configs."""
        enabled = {
            name: group
            for name, group in config.agents.items()
            if group.enabled and group.count > 0
        }
        enabled_names = set(enabled)

        if "adaptive_market_maker" in enabled_names and not (
            enabled_names & {"noise", "value"}
        ):
            warnings.warn(
                "adaptive_market_maker is enabled but no noise or value "
                "agents are present — the order book will have no background "
                "liquidity and the market maker may have no counterparties.",
                stacklevel=3,
            )

        if "pov_execution" in enabled_names:
            bg_count = sum(enabled[n].count for n in ("noise", "value") if n in enabled)
            if bg_count < 10:
                warnings.warn(
                    f"pov_execution is enabled but only {bg_count} background "
                    f"agent(s) are present. POV targeting needs meaningful "
                    f"background volume — consider adding more noise/value agents.",
                    stacklevel=3,
                )

        if config.market.start_time >= config.market.end_time:
            raise ValueError(
                f"Market start_time ({config.market.start_time}) is not before "
                f"end_time ({config.market.end_time}) — the trading window "
                f"is empty or inverted."
            )

        if "pov_execution" in enabled_names:
            from abides_markets.config_system.agent_configs import str_to_ns

            pov_params = enabled["pov_execution"].params
            start_off = pov_params.get("start_time_offset", "00:05:00")
            end_off = pov_params.get("end_time_offset", "00:05:00")
            try:
                mkt_ns = str_to_ns(config.market.end_time) - str_to_ns(
                    config.market.start_time
                )
                window_consumed = str_to_ns(start_off) + str_to_ns(end_off)
                if window_consumed >= mkt_ns:
                    warnings.warn(
                        "POV execution offsets consume the entire market "
                        "window — the execution agent will have no time to trade.",
                        stacklevel=3,
                    )
            except Exception:
                pass

        total_agents = sum(g.count for g in enabled.values())
        if total_agents > 10_000:
            warnings.warn(
                f"Total enabled agent count is {total_agents:,}. Simulations "
                f"with >10,000 agents may be very slow.",
                stacklevel=3,
            )

        if total_agents == 0:
            warnings.warn(
                "No agents are enabled — the simulation will have no participants.",
                stacklevel=3,
            )

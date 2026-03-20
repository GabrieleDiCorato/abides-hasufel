"""Tests for the declarative configuration system."""

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from abides_markets.config_system import (
    BaseAgentConfig,
    SimulationBuilder,
    SimulationConfig,
    compile,
    config_from_dict,
    config_to_dict,
    get_config_schema,
    list_agent_types,
    list_templates,
    load_config,
    register_agent,
    registry,
    save_config,
    validate_config,
)
from abides_markets.config_system.models import AgentGroupConfig


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtin_agents_registered(self):
        """All 5 built-in agent types should be registered."""
        names = registry.registered_names()
        assert "noise" in names
        assert "value" in names
        assert "momentum" in names
        assert "adaptive_market_maker" in names
        assert "pov_execution" in names

    def test_get_existing_agent(self):
        entry = registry.get("noise")
        assert entry.name == "noise"
        assert entry.category == "background"

    def test_get_unknown_agent(self):
        with pytest.raises(KeyError, match="Unknown agent type"):
            registry.get("nonexistent_agent")

    def test_list_agents_returns_schemas(self):
        agents = registry.list_agents()
        assert len(agents) >= 5
        noise = next(a for a in agents if a["name"] == "noise")
        assert "parameters" in noise
        assert "starting_cash" in noise["parameters"]

    def test_get_json_schema(self):
        schema = registry.get_json_schema("value")
        assert "properties" in schema
        assert "r_bar" in schema["properties"]
        assert "kappa" in schema["properties"]


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    def test_default_simulation_config(self):
        """SimulationConfig with all defaults should be valid."""
        config = SimulationConfig()
        assert config.market.ticker == "ABM"
        assert config.infrastructure.default_computation_delay == 50

    def test_agent_group_config_validation(self):
        """count must be >= 0."""
        with pytest.raises(Exception):
            AgentGroupConfig(count=-1)

    def test_agent_group_forbids_extra_fields(self):
        with pytest.raises(Exception):
            AgentGroupConfig(count=10, unknown_field="bad")


# ---------------------------------------------------------------------------
# Builder tests
# ---------------------------------------------------------------------------


class TestBuilder:
    def test_from_template_rmsc04(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        assert config.market.ticker == "ABM"
        assert config.agents["noise"].count == 1000
        assert config.agents["value"].count == 102
        assert config.agents["momentum"].count == 12
        assert config.agents["adaptive_market_maker"].count == 2

    def test_enable_disable_agents(self):
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .disable_agent("momentum")
            .enable_agent("noise", count=500)
            .seed(42)
            .build()
        )
        assert config.agents["momentum"].enabled is False
        assert config.agents["noise"].count == 500

    def test_market_override(self):
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .market(ticker="AAPL", date="20220101")
            .seed(42)
            .build()
        )
        assert config.market.ticker == "AAPL"
        assert config.market.date == "20220101"

    def test_template_stacking(self):
        """Stacking rmsc04 + with_execution should add POV agent."""
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .from_template("with_execution")
            .seed(42)
            .build()
        )
        assert "pov_execution" in config.agents
        assert config.agents["pov_execution"].enabled is True
        # Original agents should still be present
        assert config.agents["noise"].count == 1000

    def test_enable_agent_with_params(self):
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .enable_agent("value", count=50, r_bar=200_000)
            .seed(42)
            .build()
        )
        assert config.agents["value"].count == 50
        assert config.agents["value"].params["r_bar"] == 200_000

    def test_unknown_template_raises(self):
        with pytest.raises(KeyError, match="Unknown template"):
            SimulationBuilder().from_template("nonexistent").build()


# ---------------------------------------------------------------------------
# Compiler tests
# ---------------------------------------------------------------------------


class TestCompiler:
    def test_compile_rmsc04_produces_valid_runtime(self):
        """Compiling rmsc04 template should produce a complete runtime dict."""
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        runtime = compile(config)

        # Check all required keys
        assert "start_time" in runtime
        assert "stop_time" in runtime
        assert "agents" in runtime
        assert "agent_latency_model" in runtime
        assert "default_computation_delay" in runtime
        assert "custom_properties" in runtime
        assert "random_state_kernel" in runtime
        assert "stdout_log_level" in runtime

        # Check agent counts: 1 exchange + 1000 noise + 102 value + 12 momentum + 2 MM = 1117
        assert len(runtime["agents"]) == 1117

    def test_compile_agent_ids_sequential(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        runtime = compile(config)
        ids = [a.id for a in runtime["agents"]]
        assert ids == list(range(len(ids)))

    def test_compile_exchange_is_id_zero(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        runtime = compile(config)
        assert runtime["agents"][0].id == 0
        assert runtime["agents"][0].type == "ExchangeAgent"

    def test_compile_deterministic_seed(self):
        """Same seed should produce identical agent counts."""
        config1 = SimulationBuilder().from_template("rmsc04").seed(42).build()
        config2 = SimulationBuilder().from_template("rmsc04").seed(42).build()
        runtime1 = compile(config1)
        runtime2 = compile(config2)
        assert len(runtime1["agents"]) == len(runtime2["agents"])

    def test_compile_disabled_agents_excluded(self):
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .disable_agent("momentum")
            .disable_agent("adaptive_market_maker")
            .seed(42)
            .build()
        )
        runtime = compile(config)
        # 1 exchange + 1000 noise + 102 value = 1103
        assert len(runtime["agents"]) == 1103

    def test_compile_with_execution_agent(self):
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .from_template("with_execution")
            .seed(42)
            .build()
        )
        runtime = compile(config)
        # 1117 + 1 execution = 1118
        assert len(runtime["agents"]) == 1118
        # Last agent should be the execution agent
        last_agent = runtime["agents"][-1]
        assert last_agent.type == "ExecutionAgent"

    def test_compile_oracle_is_set(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        runtime = compile(config)
        oracle = runtime["custom_properties"]["oracle"]
        assert oracle is not None

    def test_compile_empty_agents(self):
        """Should work with no agents enabled (just the exchange)."""
        config = SimulationConfig(
            simulation={"seed": 42},
        )
        runtime = compile(config)
        assert len(runtime["agents"]) == 1  # just exchange
        assert runtime["agents"][0].type == "ExchangeAgent"


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_roundtrip_json(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = Path(f.name)

        try:
            save_config(config, path)
            loaded = load_config(path)
            assert loaded.market.ticker == config.market.ticker
            assert loaded.agents["noise"].count == config.agents["noise"].count
            assert loaded.simulation.seed == config.simulation.seed
        finally:
            path.unlink(missing_ok=True)

    def test_roundtrip_yaml(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            path = Path(f.name)

        try:
            save_config(config, path)
            loaded = load_config(path)
            assert loaded.market.ticker == config.market.ticker
            assert loaded.agents["noise"].count == config.agents["noise"].count
        finally:
            path.unlink(missing_ok=True)

    def test_config_to_from_dict(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        d = config_to_dict(config)
        assert isinstance(d, dict)
        # Should be JSON-serializable
        json.dumps(d)
        # Round-trip
        restored = config_from_dict(d)
        assert restored.market.ticker == config.market.ticker

    def test_json_serializable(self):
        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        d = config_to_dict(config)
        serialized = json.dumps(d)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# Template tests
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_list_templates(self):
        templates = list_templates()
        names = [t["name"] for t in templates]
        assert "rmsc04" in names
        assert "liquid_market" in names
        assert "thin_market" in names
        assert "with_momentum" in names
        assert "with_execution" in names

    def test_liquid_market_template(self):
        config = SimulationBuilder().from_template("liquid_market").seed(42).build()
        assert config.agents["noise"].count == 5000
        assert config.agents["adaptive_market_maker"].count == 4


# ---------------------------------------------------------------------------
# Discoverability API tests
# ---------------------------------------------------------------------------


class TestDiscoverability:
    def test_list_agent_types(self):
        types = list_agent_types()
        assert len(types) >= 5
        names = [t["name"] for t in types]
        assert "noise" in names
        assert "value" in names

    def test_get_config_schema(self):
        schema = get_config_schema()
        assert "properties" in schema
        assert "market" in schema["properties"]
        assert "agents" in schema["properties"]
        assert "infrastructure" in schema["properties"]
        assert "simulation" in schema["properties"]

    def test_validate_config_valid(self):
        result = validate_config({"simulation": {"seed": 42}})
        assert result["valid"] is True

    def test_validate_config_invalid(self):
        result = validate_config(
            {"agents": {"noise": {"count": -1}}}
        )
        assert result["valid"] is False
        assert "errors" in result


# ---------------------------------------------------------------------------
# Equivalence test: new system vs rmsc04.build_config()
# ---------------------------------------------------------------------------


class TestEquivalence:
    def test_agent_counts_match_rmsc04(self):
        """New config system should produce same agent counts as rmsc04.build_config()."""
        from abides_markets.configs.rmsc04 import build_config

        seed = 42
        old_config = build_config(seed=seed)
        old_agent_count = len(old_config["agents"])

        new_config = SimulationBuilder().from_template("rmsc04").seed(seed).build()
        new_runtime = compile(new_config)
        new_agent_count = len(new_runtime["agents"])

        assert new_agent_count == old_agent_count

    def test_agent_types_match_rmsc04(self):
        """Agent type composition should match rmsc04."""
        from collections import Counter

        from abides_markets.configs.rmsc04 import build_config

        seed = 42
        old_config = build_config(seed=seed)
        old_types = Counter(type(a).__name__ for a in old_config["agents"])

        new_config = SimulationBuilder().from_template("rmsc04").seed(seed).build()
        new_runtime = compile(new_config)
        new_types = Counter(type(a).__name__ for a in new_runtime["agents"])

        assert old_types == new_types

    def test_runtime_dict_keys_match(self):
        """Runtime dict should have the same keys as rmsc04.build_config()."""
        from abides_markets.configs.rmsc04 import build_config

        old_config = build_config(seed=42)
        new_runtime = compile(
            SimulationBuilder().from_template("rmsc04").seed(42).build()
        )

        old_keys = set(old_config.keys())
        new_keys = set(new_runtime.keys())
        # New system should have at least all old keys
        assert old_keys.issubset(new_keys)


# ---------------------------------------------------------------------------
# Gym compatibility test
# ---------------------------------------------------------------------------


class TestGymCompatibility:
    def test_config_add_agents_works(self):
        """config_add_agents() should work on compiled output."""
        from abides_markets.utils import config_add_agents

        config = SimulationBuilder().from_template("rmsc04").seed(42).build()
        runtime = compile(config)
        original_count = len(runtime["agents"])

        # Simulate what gym does: add a mock agent
        from abides_markets.agents import NoiseAgent

        mock_agent = NoiseAgent(
            id=original_count,
            wakeup_time=runtime["start_time"] + 1_000_000_000,
            symbol="ABM",
            random_state=np.random.RandomState(99),
        )

        rng = np.random.RandomState(123)
        updated = config_add_agents(runtime, [mock_agent], rng)

        assert len(updated["agents"]) == original_count + 1
        assert updated["agent_latency_model"] is not None


# ---------------------------------------------------------------------------
# Custom agent registration test
# ---------------------------------------------------------------------------


class TestCustomRegistration:
    def test_register_and_use_custom_agent(self):
        """A third-party agent should be registrable and compilable."""
        from pydantic import Field

        from abides_markets.config_system.agent_configs import (
            AgentCreationContext,
            BaseAgentConfig,
        )

        # Define a simple custom agent config
        class DummyAgentConfig(BaseAgentConfig):
            threshold: float = Field(default=0.05)

            def create_agents(self, count, id_start, master_rng, context):
                from abides_markets.agents import NoiseAgent

                return [
                    NoiseAgent(
                        id=id_start + i,
                        wakeup_time=context.mkt_open + 1_000_000_000,
                        symbol=context.ticker,
                        starting_cash=self.starting_cash,
                        random_state=np.random.RandomState(
                            seed=master_rng.randint(low=0, high=2**32, dtype="uint64")
                        ),
                    )
                    for i in range(count)
                ]

        # Register it
        registry.register(
            "_test_dummy",
            DummyAgentConfig,
            category="strategy",
            description="Test dummy agent",
        )

        try:
            # Build and compile a config using the custom agent
            config = (
                SimulationBuilder()
                .from_template("rmsc04")
                .enable_agent("_test_dummy", count=5, threshold=0.1)
                .seed(42)
                .build()
            )
            runtime = compile(config)
            # 1117 (rmsc04) + 5 dummy = 1122
            assert len(runtime["agents"]) == 1122
        finally:
            # Clean up: remove from registry
            if "_test_dummy" in registry._entries:
                del registry._entries["_test_dummy"]

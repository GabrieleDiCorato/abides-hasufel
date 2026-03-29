"""Tests for VWAPExecutionAgent (P1 item 6).

Covers:
- Default U-shaped profile generation
- Custom profile (normalisation, truncation, padding)
- Slice sizing proportional to profile weights
- Catch-up after partial fills
- Uniform degradation when profile is None with 1 slice
- Config validation and builder integration
"""

from __future__ import annotations

import numpy as np
import pytest

from abides_core.utils import str_to_ns

MKT_OPEN: int = str_to_ns("09:30:00")
MKT_CLOSE: int = str_to_ns("16:00:00")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _make_agent(quantity: int = 1000, freq_str: str = "1min", **kw):
    from abides_markets.agents.vwap_execution_agent import VWAPExecutionAgent

    start = MKT_OPEN + str_to_ns("00:30:00")
    end = MKT_CLOSE - str_to_ns("00:30:00")
    return VWAPExecutionAgent(
        id=0,
        symbol="TEST",
        starting_cash=10_000_000,
        start_time=start,
        end_time=end,
        freq=str_to_ns(freq_str),
        quantity=quantity,
        random_state=np.random.RandomState(42),
        **kw,
    )


# ---------------------------------------------------------------------------
# Profile generation / normalisation
# ---------------------------------------------------------------------------
class TestVWAPProfile:
    def test_default_u_profile_sums_to_one(self):
        agent = _make_agent()
        assert abs(sum(agent.volume_profile) - 1.0) < 1e-9

    def test_default_u_profile_length_matches_slices(self):
        agent = _make_agent()
        assert len(agent.volume_profile) == agent.total_slices

    def test_u_shape_ends_higher_than_middle(self):
        agent = _make_agent()
        p = agent.volume_profile
        mid = len(p) // 2
        assert p[0] > p[mid]
        assert p[-1] > p[mid]

    def test_custom_profile_normalised(self):
        agent = _make_agent(volume_profile=[2.0, 1.0, 3.0])
        assert abs(sum(agent.volume_profile) - 1.0) < 1e-9

    def test_custom_profile_padded_when_short(self):
        """A 3-element profile is padded to total_slices entries."""
        agent = _make_agent(volume_profile=[1.0, 2.0, 3.0])
        assert len(agent.volume_profile) == agent.total_slices

    def test_custom_profile_truncated_when_long(self):
        """A profile longer than total_slices is truncated."""
        long_profile = [1.0] * 5000
        agent = _make_agent(volume_profile=long_profile)
        assert len(agent.volume_profile) == agent.total_slices

    def test_single_slice_profile(self):
        from abides_markets.agents.vwap_execution_agent import _default_u_profile

        p = _default_u_profile(1)
        assert p == [1.0]


# ---------------------------------------------------------------------------
# Slice sizing
# ---------------------------------------------------------------------------
class TestVWAPSliceSizing:
    def test_first_slice_proportional_to_weight(self):
        # Use a small number of slices for easy verification
        agent = _make_agent(quantity=100, freq_str="5h30min")
        # Should have exactly 1 slice
        assert agent.total_slices == 1
        qty = agent._compute_slice_quantity(agent.start_time)
        assert qty == 100  # single-slice gets everything

    def test_two_slices_with_equal_profile(self):
        """Two equal-weight slices → each gets ~50% of quantity."""
        agent = _make_agent(
            quantity=100,
            freq_str="2h45min",  # 330min / 165min ≈ 2 slices
            volume_profile=[1.0, 1.0],
        )
        assert agent.total_slices == 2
        first = agent._compute_slice_quantity(agent.start_time)
        assert first == 50  # half

    def test_two_slices_asymmetric(self):
        """Weight [3, 1] → first slice gets 75% of quantity (with rounding)."""
        agent = _make_agent(
            quantity=100,
            freq_str="2h45min",
            volume_profile=[3.0, 1.0],
        )
        first = agent._compute_slice_quantity(agent.start_time)
        assert first == 75

    def test_catchup_redistributes(self):
        """Partial fill on first slice → second slice adjusts."""
        agent = _make_agent(
            quantity=100,
            freq_str="2h45min",
            volume_profile=[1.0, 1.0],
        )
        first = agent._compute_slice_quantity(agent.start_time)
        # Simulate 50% fill of first slice
        filled = first // 2
        agent.executed_quantity += filled
        agent.remaining_quantity -= filled

        second = agent._compute_slice_quantity(agent.start_time + agent.freq)
        # Second should be all remaining
        assert second == agent.remaining_quantity

    def test_past_profile_returns_remainder(self):
        agent = _make_agent(quantity=50, freq_str="1min")
        agent.slice_index = len(agent.volume_profile) + 5
        agent.remaining_quantity = 17
        qty = agent._compute_slice_quantity(agent.start_time)
        assert qty == 17


# ---------------------------------------------------------------------------
# Config-system tests
# ---------------------------------------------------------------------------
class TestVWAPConfig:
    def test_config_creates_agent(self):
        from abides_markets.agents.vwap_execution_agent import VWAPExecutionAgent
        from abides_markets.config_system.agent_configs import (
            AgentCreationContext,
            VWAPExecutionAgentConfig,
        )

        cfg = VWAPExecutionAgentConfig(quantity=5000)
        ctx = AgentCreationContext(
            ticker="TEST",
            mkt_open=MKT_OPEN,
            mkt_close=MKT_CLOSE,
            log_orders=False,
            oracle_r_bar=None,
        )
        agents = cfg.create_agents(
            count=1,
            id_start=100,
            master_rng=np.random.RandomState(1),
            context=ctx,
        )
        assert len(agents) == 1
        assert isinstance(agents[0], VWAPExecutionAgent)
        assert agents[0].quantity == 5000
        assert agents[0].order_style == "ioc_limit"

    def test_custom_profile_passed_through(self):
        from abides_markets.config_system.agent_configs import (
            AgentCreationContext,
            VWAPExecutionAgentConfig,
        )

        cfg = VWAPExecutionAgentConfig(
            quantity=100,
            volume_profile=[1.0, 2.0, 1.0],
        )
        ctx = AgentCreationContext(
            ticker="TEST",
            mkt_open=MKT_OPEN,
            mkt_close=MKT_CLOSE,
            log_orders=False,
            oracle_r_bar=None,
        )
        agents = cfg.create_agents(
            count=1,
            id_start=0,
            master_rng=np.random.RandomState(1),
            context=ctx,
        )
        # Profile should have been normalised to total_slices length
        assert abs(sum(agents[0].volume_profile) - 1.0) < 1e-9

    def test_inverted_window_raises(self):
        from abides_markets.config_system.agent_configs import (
            AgentCreationContext,
            VWAPExecutionAgentConfig,
        )

        cfg = VWAPExecutionAgentConfig(
            start_time_offset="05:00:00",
            end_time_offset="05:00:00",
        )
        ctx = AgentCreationContext(
            ticker="TEST",
            mkt_open=MKT_OPEN,
            mkt_close=MKT_CLOSE,
            log_orders=False,
            oracle_r_bar=None,
        )
        with pytest.raises(ValueError, match="inverted"):
            cfg.create_agents(
                count=1,
                id_start=0,
                master_rng=np.random.RandomState(1),
                context=ctx,
            )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestVWAPRegistration:
    def test_registered_in_registry(self):
        from abides_markets.config_system.registry import registry

        entry = registry.get("vwap_execution")
        assert entry is not None
        assert entry.category == "execution"

    def test_builder_integration(self):
        from abides_markets.config_system.builder import SimulationBuilder

        config = (
            SimulationBuilder()
            .market(oracle=None, opening_price=100_000)
            .seed(42)
            .enable_agent("vwap_execution", count=1, quantity=500)
            .build()
        )
        assert "vwap_execution" in config.agents
        assert config.agents["vwap_execution"].enabled is True

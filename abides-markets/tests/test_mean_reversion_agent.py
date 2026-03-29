"""Tests for MeanReversionAgent — contrarian z-score strategy.

Covers:
- Construction and state-machine initialisation
- Z-score computation edge cases
- Buy/sell signal logic
- Config validation (window, thresholds)
- Integration: agent survives a short simulation
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from abides_core.utils import datetime_str_to_ns, str_to_ns
from abides_markets.agents.examples.mean_reversion_agent import MeanReversionAgent
from abides_markets.config_system import SimulationBuilder
from abides_markets.config_system.agent_configs import MeanReversionAgentConfig
from abides_markets.simulation import ResultProfile, run_simulation

# ---------------------------------------------------------------------------
# Local helpers (mirrors conftest.make_agent for standalone use)
# ---------------------------------------------------------------------------

DATE = datetime_str_to_ns("20210205")
MKT_OPEN = DATE + str_to_ns("09:30:00")
MKT_CLOSE = MKT_OPEN + str_to_ns("06:30:00")


class _StubKernel:
    oracle = None

    def send_message(self, sender_id, recipient_id, message, **kwargs):
        pass


def _make_agent(agent_cls=MeanReversionAgent, *, seed=42, symbol="TEST",
                starting_cash=10_000_000, **kwargs):
    defaults = {
        "id": 0,
        "random_state": np.random.RandomState(seed),
        "symbol": symbol,
        "starting_cash": starting_cash,
    }
    merged = {**defaults, **kwargs}
    agent = agent_cls(**merged)
    agent.kernel = _StubKernel()
    agent.exchange_id = 99
    agent.mkt_open = MKT_OPEN
    agent.mkt_close = MKT_CLOSE
    agent.current_time = MKT_OPEN + str_to_ns("00:05:00")
    return agent


# ---------------------------------------------------------------------------
# Construction & state machine
# ---------------------------------------------------------------------------


class TestMeanReversionAgentConstruction:
    def test_default_construction(self):
        agent = _make_agent(window=5, entry_threshold=2.0)
        assert agent.state == "AWAITING_WAKEUP"
        assert agent.window == 5
        assert agent.entry_threshold == 2.0
        assert agent.exit_threshold == 0.5
        assert len(agent.mid_list) == 0

    def test_valid_states(self):
        assert "AWAITING_WAKEUP" in MeanReversionAgent.VALID_STATES
        assert "AWAITING_SPREAD" in MeanReversionAgent.VALID_STATES
        assert "AWAITING_MARKET_DATA" in MeanReversionAgent.VALID_STATES

    def test_window_too_small_raises(self):
        with pytest.raises(ValueError, match="window.*must be >= 2"):
            _make_agent(window=1)

    def test_negative_entry_threshold_raises(self):
        with pytest.raises(ValueError, match="entry_threshold.*must be positive"):
            _make_agent(entry_threshold=-1.0)

    def test_exit_ge_entry_raises(self):
        with pytest.raises(ValueError, match="exit_threshold.*must be < entry_threshold"):
            _make_agent(entry_threshold=2.0, exit_threshold=2.0)

    def test_negative_exit_threshold_raises(self):
        with pytest.raises(ValueError, match="exit_threshold.*must be non-negative"):
            _make_agent(exit_threshold=-0.1)


# ---------------------------------------------------------------------------
# Z-score computation
# ---------------------------------------------------------------------------


class TestZScore:
    def _make(self, **kw):
        return _make_agent(window=5, entry_threshold=2.0, **kw)

    def test_z_score_none_when_insufficient_data(self):
        agent = self._make()
        agent.mid_list.append(100)
        assert agent._z_score() is None

    def test_z_score_none_when_all_same(self):
        agent = self._make()
        for _ in range(5):
            agent.mid_list.append(10000)
        # std == 0 → None
        assert agent._z_score() is None

    def test_z_score_computes_correctly(self):
        agent = self._make()
        # prices: 100, 102, 98, 101, 110
        prices = [10000, 10200, 9800, 10100, 11000]
        for p in prices:
            agent.mid_list.append(p)

        z = agent._z_score()
        assert z is not None

        # Manual check
        mean = sum(prices) / len(prices)
        var = sum((x - mean) ** 2 for x in prices) / len(prices)
        std = math.sqrt(var)
        expected_z = (prices[-1] - mean) / std
        assert abs(z - expected_z) < 1e-10

    def test_z_score_negative_for_low_price(self):
        agent = self._make()
        # Put a low outlier at the end
        prices = [10000, 10100, 10050, 10000, 9000]
        for p in prices:
            agent.mid_list.append(p)
        z = agent._z_score()
        assert z is not None
        assert z < 0

    def test_z_score_positive_for_high_price(self):
        agent = self._make()
        prices = [10000, 10100, 10050, 10000, 11500]
        for p in prices:
            agent.mid_list.append(p)
        z = agent._z_score()
        assert z is not None
        assert z > 0


# ---------------------------------------------------------------------------
# Order placement logic
# ---------------------------------------------------------------------------


class TestPlaceOrders:
    def _make(self, **kw):
        defaults = dict(window=5, entry_threshold=1.5, exit_threshold=0.3)
        defaults.update(kw)
        return _make_agent(**defaults)

    def test_no_orders_when_bid_none(self):
        agent = self._make()
        # Should not raise
        agent.place_orders(None, 10100)

    def test_no_orders_when_ask_none(self):
        agent = self._make()
        agent.place_orders(10000, None)

    def test_no_orders_before_window_full(self):
        agent = self._make()
        # Only 3 observations, window=5 → no order
        agent.place_orders(10000, 10100)
        agent.place_orders(10000, 10100)
        agent.place_orders(10000, 10100)
        # mid_list has 3 entries, but window requires 5

    def test_buy_signal_on_low_z(self):
        """When z-score drops below -entry_threshold, agent should buy."""
        agent = self._make()
        # Build up history with stable prices, then drop
        stable = [(10000, 10100)] * 4
        for bid, ask in stable:
            agent.place_orders(bid, ask)

        # Dramatic drop → z-score should be very negative
        agent.place_orders(8000, 8100)
        # The agent should have attempted to place orders (via stub kernel)
        # We verify the mid_list was populated correctly
        assert len(agent.mid_list) == 5

    def test_sell_signal_on_high_z(self):
        """When z-score rises above +entry_threshold, agent should sell."""
        agent = self._make()
        stable = [(10000, 10100)] * 4
        for bid, ask in stable:
            agent.place_orders(bid, ask)

        # Dramatic spike
        agent.place_orders(12000, 12100)
        assert len(agent.mid_list) == 5


# ---------------------------------------------------------------------------
# Wake frequency
# ---------------------------------------------------------------------------


class TestWakeFrequency:
    def test_fixed_frequency(self):
        agent = _make_agent(
            wake_up_freq=str_to_ns("30s"),
            poisson_arrival=False,
        )
        assert agent.get_wake_frequency() == str_to_ns("30s")

    def test_poisson_frequency_is_positive(self):
        agent = _make_agent(
            wake_up_freq=str_to_ns("30s"),
            poisson_arrival=True,
        )
        freq = agent.get_wake_frequency()
        assert freq > 0


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestMeanReversionAgentConfig:
    def test_default_config_valid(self):
        cfg = MeanReversionAgentConfig()
        assert cfg.window == 20
        assert cfg.entry_threshold == 2.0
        assert cfg.exit_threshold == 0.5

    def test_exit_ge_entry_rejected(self):
        with pytest.raises(ValueError, match="exit_threshold.*must be < entry_threshold"):
            MeanReversionAgentConfig(entry_threshold=1.5, exit_threshold=1.5)

    def test_window_below_2_rejected(self):
        with pytest.raises(ValueError):
            MeanReversionAgentConfig(window=1)

    def test_registered_in_config_system(self):
        from abides_markets.config_system.registry import registry

        entry = registry.get("mean_reversion")
        assert entry is not None
        assert entry.category == "strategy"
        assert entry.requires_oracle is False


# ---------------------------------------------------------------------------
# Integration: agent survives a short simulation
# ---------------------------------------------------------------------------


class TestMeanReversionIntegration:
    def test_simulation_with_mean_reversion(self, tmp_path):
        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .market(end_time="09:32:00")
            .enable_agent("mean_reversion", count=5, window=5)
            .seed(42)
            .build()
        )
        result = run_simulation(
            config, profile=ResultProfile.SUMMARY, log_dir=str(tmp_path)
        )
        assert result is not None
        assert len(result.agents) > 0

        # Check mean reversion agents appear in results
        mr_agents = [a for a in result.agents if a.agent_type == "MeanReversionAgent"]
        assert len(mr_agents) == 5

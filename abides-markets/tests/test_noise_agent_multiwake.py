"""Tests for NoiseAgent multi-wake mode (P0 item 2).

Covers:
- Single-shot mode unchanged (regression)
- Multi-wake mode: agent schedules next wakeup after placing order
- Market-close prevents re-scheduling
- get_wake_frequency Poisson vs fixed
- Config validation for multi_wake fields
- Integration: multi-wake noise agents produce sustained flow
"""

from __future__ import annotations

from typing import Any

import numpy as np

from abides_core import NanosecondTime
from abides_core.utils import str_to_ns
from abides_markets.agents.noise_agent import NoiseAgent
from abides_markets.messages.query import QuerySpreadResponseMsg

# ---------------------------------------------------------------------------
# Time constants (same as conftest)
# ---------------------------------------------------------------------------

DATE: NanosecondTime = 1_612_483_200_000_000_000  # 2021-02-05 00:00 UTC
MKT_OPEN: NanosecondTime = DATE + str_to_ns("09:30:00")
MKT_CLOSE: NanosecondTime = MKT_OPEN + str_to_ns("06:30:00")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubKernel:
    oracle = None

    def __init__(self):
        self.messages: list[tuple] = []
        self.wakeups: list[NanosecondTime] = []

    def send_message(
        self, sender_id: int, recipient_id: int, message: Any, **kwargs
    ) -> None:
        self.messages.append((sender_id, recipient_id, message))


def _make_agent(seed: int = 42, **kwargs: Any) -> NoiseAgent:
    defaults: dict[str, Any] = {
        "id": 0,
        "random_state": np.random.RandomState(seed),
        "symbol": "TEST",
        "starting_cash": 10_000_000,
        "wakeup_time": MKT_OPEN + str_to_ns("00:05:00"),
    }
    merged = {**defaults, **kwargs}
    agent = NoiseAgent(**merged)
    agent.kernel = _StubKernel()  # type: ignore[assignment]
    agent.exchange_id = 99
    agent.mkt_open = MKT_OPEN
    agent.mkt_close = MKT_CLOSE
    agent.current_time = MKT_OPEN + str_to_ns("00:05:00")
    return agent


def _spread_response(bid: int = 10_000, ask: int = 10_100) -> QuerySpreadResponseMsg:
    return QuerySpreadResponseMsg(
        symbol="TEST",
        depth=1,
        bids=[(bid, 100)],
        asks=[(ask, 100)],
        last_trade=bid,
        mkt_closed=False,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestNoiseAgentConstruction:
    def test_default_single_shot(self):
        agent = _make_agent()
        assert agent.multi_wake is False
        assert agent.state == "AWAITING_WAKEUP"

    def test_multi_wake_construction(self):
        agent = _make_agent(multi_wake=True, wake_up_freq=str_to_ns("15s"))
        assert agent.multi_wake is True
        assert agent.wake_up_freq == str_to_ns("15s")
        assert agent.poisson_arrival is True

    def test_multi_wake_fixed_interval(self):
        agent = _make_agent(multi_wake=True, poisson_arrival=False)
        assert agent.poisson_arrival is False


# ---------------------------------------------------------------------------
# get_wake_frequency
# ---------------------------------------------------------------------------


class TestWakeFrequency:
    def test_single_shot_returns_tiny(self):
        """In single-shot mode, get_wake_frequency returns randint(0, 100)."""
        agent = _make_agent()
        freq = agent.get_wake_frequency()
        assert 0 <= freq < 100

    def test_multi_wake_fixed(self):
        freq_ns = str_to_ns("20s")
        agent = _make_agent(
            multi_wake=True, wake_up_freq=freq_ns, poisson_arrival=False
        )
        assert agent.get_wake_frequency() == freq_ns

    def test_multi_wake_poisson_positive(self):
        agent = _make_agent(
            multi_wake=True, wake_up_freq=str_to_ns("30s"), poisson_arrival=True
        )
        freqs = [agent.get_wake_frequency() for _ in range(50)]
        assert all(f >= 0 for f in freqs)
        # Mean should be roughly 30s; at least not all zero or all identical.
        assert len(set(freqs)) > 1

    def test_multi_wake_poisson_mean(self):
        """Poisson mean should be close to wake_up_freq over many samples."""
        freq_ns = str_to_ns("30s")
        agent = _make_agent(multi_wake=True, wake_up_freq=freq_ns, poisson_arrival=True)
        freqs = [agent.get_wake_frequency() for _ in range(5000)]
        mean = np.mean(freqs)
        assert abs(mean - freq_ns) / freq_ns < 0.1  # within 10%


# ---------------------------------------------------------------------------
# Single-shot regression
# ---------------------------------------------------------------------------


class TestSingleShotRegression:
    def test_no_reschedule_after_order(self):
        """In default mode, agent does NOT schedule a next wakeup after placing order."""
        agent = _make_agent()
        agent.state = "AWAITING_SPREAD"

        # Patch set_wakeup to record calls.
        wakeup_calls: list[NanosecondTime] = []
        agent.set_wakeup = lambda t: wakeup_calls.append(t)

        # Inject known spread so place_order can succeed.
        agent.known_bids["TEST"] = [(10_000, 100)]
        agent.known_asks["TEST"] = [(10_100, 100)]

        current = MKT_OPEN + str_to_ns("00:10:00")
        agent.receive_message(current, 99, _spread_response())

        assert agent.state == "AWAITING_WAKEUP"
        assert len(wakeup_calls) == 0  # No reschedule


# ---------------------------------------------------------------------------
# Multi-wake mode
# ---------------------------------------------------------------------------


class TestMultiWakeMode:
    def test_reschedules_after_order(self):
        """In multi-wake mode, agent schedules next wakeup after placing order."""
        agent = _make_agent(
            multi_wake=True, wake_up_freq=str_to_ns("10s"), poisson_arrival=False
        )
        agent.state = "AWAITING_SPREAD"

        wakeup_calls: list[NanosecondTime] = []
        agent.set_wakeup = lambda t: wakeup_calls.append(t)

        agent.known_bids["TEST"] = [(10_000, 100)]
        agent.known_asks["TEST"] = [(10_100, 100)]

        current = MKT_OPEN + str_to_ns("00:10:00")
        agent.receive_message(current, 99, _spread_response())

        assert agent.state == "AWAITING_WAKEUP"
        assert len(wakeup_calls) == 1
        expected = current + str_to_ns("10s")
        assert wakeup_calls[0] == expected

    def test_reschedules_poisson(self):
        """In multi-wake Poisson mode, next wakeup is randomized."""
        agent = _make_agent(
            multi_wake=True, wake_up_freq=str_to_ns("30s"), poisson_arrival=True
        )
        agent.state = "AWAITING_SPREAD"

        wakeup_calls: list[NanosecondTime] = []
        agent.set_wakeup = lambda t: wakeup_calls.append(t)

        agent.known_bids["TEST"] = [(10_000, 100)]
        agent.known_asks["TEST"] = [(10_100, 100)]

        current = MKT_OPEN + str_to_ns("00:10:00")
        agent.receive_message(current, 99, _spread_response())

        assert len(wakeup_calls) == 1
        assert wakeup_calls[0] > current  # Next wakeup is in the future

    def test_no_reschedule_when_market_closed(self):
        """Multi-wake does not reschedule when market is closed."""
        agent = _make_agent(multi_wake=True)
        agent.state = "AWAITING_SPREAD"
        agent.mkt_closed = True

        wakeup_calls: list[NanosecondTime] = []
        agent.set_wakeup = lambda t: wakeup_calls.append(t)

        current = MKT_CLOSE + str_to_ns("00:01:00")
        agent.receive_message(current, 99, _spread_response())

        # mkt_closed early-return prevents both place_order and set_wakeup.
        assert len(wakeup_calls) == 0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestNoiseAgentConfig:
    def test_default_single_shot_config(self):
        from abides_markets.config_system.agent_configs import NoiseAgentConfig

        cfg = NoiseAgentConfig()
        assert cfg.multi_wake is False
        assert cfg.wake_up_freq == "30s"
        assert cfg.poisson_arrival is True

    def test_multi_wake_config(self):
        from abides_markets.config_system.agent_configs import NoiseAgentConfig

        cfg = NoiseAgentConfig(
            multi_wake=True, wake_up_freq="15s", poisson_arrival=False
        )
        assert cfg.multi_wake is True
        assert cfg.wake_up_freq == "15s"
        assert cfg.poisson_arrival is False

    def test_config_backward_compat(self):
        """Existing configs without multi_wake fields still work."""
        from abides_markets.config_system.agent_configs import NoiseAgentConfig

        cfg = NoiseAgentConfig(noise_mkt_open_offset="-00:15:00")
        assert cfg.multi_wake is False


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestMultiWakeIntegration:
    def test_multi_wake_simulation(self):
        """Multi-wake noise agents run without crash in a full simulation."""
        from abides_markets.config_system import SimulationBuilder
        from abides_markets.simulation import run_simulation

        config = (
            SimulationBuilder()
            .from_template("rmsc04")
            .market(end_time="09:32:00")
            .seed(42)
            .enable_agent("noise", count=100, multi_wake=True, wake_up_freq="5s")
            .build()
        )
        result = run_simulation(config)
        assert result is not None

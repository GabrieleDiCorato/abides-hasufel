"""Tests for cross-interactions between risk guard mechanisms.

Verifies that circuit breaker, position limit, and drawdown interact
correctly when multiple guards are active simultaneously.
"""

import numpy as np

from abides_core.utils import datetime_str_to_ns, str_to_ns
from abides_markets.agents.trading_agent import TradingAgent
from abides_markets.orders import Side

DATE = datetime_str_to_ns("20210205")
MKT_OPEN = DATE + str_to_ns("09:30:00")
MKT_CLOSE = MKT_OPEN + str_to_ns("06:30:00")
SYMBOL = "TEST"


def _make_agent(
    position_limit: int | None = None,
    position_limit_clamp: bool = False,
    max_drawdown: int | None = None,
    max_order_rate: int | None = None,
    order_rate_window_ns: int = 60_000_000_000,
    starting_cash: int = 10_000_000,
) -> TradingAgent:
    agent = TradingAgent(
        id=0,
        random_state=np.random.RandomState(42),
        starting_cash=starting_cash,
        position_limit=position_limit,
        position_limit_clamp=position_limit_clamp,
        max_drawdown=max_drawdown,
        max_order_rate=max_order_rate,
        order_rate_window_ns=order_rate_window_ns,
    )
    agent.exchange_id = 99
    agent.current_time = MKT_OPEN
    agent.mkt_open = MKT_OPEN
    agent.mkt_close = MKT_CLOSE
    agent.send_message = lambda *a, **kw: None  # type: ignore[method-assign]
    agent.send_message_batch = lambda *a, **kw: None  # type: ignore[method-assign]
    agent.logEvent = lambda *a, **kw: None  # type: ignore[method-assign]
    return agent


class TestCircuitBreakerSupersedesPositionLimit:
    """Circuit breaker fires first — position limit is never reached."""

    def test_tripped_breaker_blocks_despite_room(self):
        """Position limit has plenty of room, but breaker is tripped → None."""
        agent = _make_agent(position_limit=100, max_drawdown=500_000)
        # Trip the breaker manually.
        agent._circuit_breaker_tripped = True
        result = agent.create_limit_order(SYMBOL, 10, Side.BID, 10_000)
        assert result is None

    def test_order_rate_trips_before_position_check(self):
        """Exceed order rate → breaker trips, position limit irrelevant."""
        agent = _make_agent(position_limit=100, max_order_rate=2)
        agent._window_start = MKT_OPEN
        agent._order_count_in_window = 2  # Already at limit

        result = agent.create_limit_order(SYMBOL, 10, Side.BID, 10_000)
        assert result is None
        assert agent._circuit_breaker_tripped is True

    def test_drawdown_trips_before_position_check(self):
        """Drawdown exceeds max → breaker trips before position limit runs."""
        agent = _make_agent(position_limit=100, max_drawdown=100_000)
        # Setup a position losing money.
        agent.holdings[SYMBOL] = 100
        agent.holdings["CASH"] = 10_000_000 - 100 * 10_000
        agent.last_trade[SYMBOL] = 8_000  # lost 200_000 > 100_000

        result = agent.create_limit_order(SYMBOL, 10, Side.BID, 10_000)
        assert result is None
        assert agent._circuit_breaker_tripped is True


class TestPositionLimitAfterBreakerOK:
    """When breaker is not tripped, position limit still enforces."""

    def test_both_guards_active_order_within_both(self):
        """Both guards active; order fits both → allowed."""
        agent = _make_agent(position_limit=100, max_drawdown=500_000)
        order = agent.create_limit_order(SYMBOL, 10, Side.BID, 10_000)
        assert order is not None
        assert order.quantity == 10

    def test_both_guards_active_position_over_limit(self):
        """Position over limit, breaker fine → position limit blocks."""
        agent = _make_agent(position_limit=100, max_drawdown=500_000)
        agent.holdings[SYMBOL] = 100  # at limit
        result = agent.create_limit_order(SYMBOL, 10, Side.BID, 10_000)
        assert result is None

    def test_position_limit_clamp_with_active_breaker_ok(self):
        """Clamp mode + active breaker → order clamped, not breaker-killed."""
        agent = _make_agent(
            position_limit=100,
            position_limit_clamp=True,
            max_drawdown=500_000,
        )
        agent.holdings[SYMBOL] = 90
        order = agent.create_limit_order(SYMBOL, 20, Side.BID, 10_000)
        assert order is not None
        assert order.quantity == 10  # clamped from 20 to 10


class TestDrawdownEdgeCases:
    """Edge cases around drawdown computation: exact boundary, multi-symbol."""

    def test_loss_exactly_equal_to_max_drawdown_trips(self):
        """loss == max_drawdown → trips (>=, not just >)."""
        agent = _make_agent(max_drawdown=200_000)
        agent.holdings[SYMBOL] = 100
        agent.holdings["CASH"] = 10_000_000 - 100 * 10_000
        # last_trade at 8000: mark = 9_000_000 + 800_000 = 9_800_000
        # loss = 10_000_000 - 9_800_000 = 200_000 == max_drawdown
        agent.last_trade[SYMBOL] = 8_000
        assert agent._check_circuit_breaker() is True

    def test_loss_one_below_max_drawdown_ok(self):
        """loss == max_drawdown - 1 → does NOT trip."""
        agent = _make_agent(max_drawdown=200_001)
        agent.holdings[SYMBOL] = 100
        agent.holdings["CASH"] = 10_000_000 - 100 * 10_000
        agent.last_trade[SYMBOL] = 8_000  # loss = 200_000 < 200_001
        assert agent._check_circuit_breaker() is False

    def test_multi_symbol_drawdown(self):
        """Mark-to-market includes all symbols for drawdown check."""
        agent = _make_agent(max_drawdown=300_000)
        # 50 shares of A at cost 10_000, now 8_000 → loss 100k
        agent.holdings["A"] = 50
        agent.last_trade["A"] = 8_000
        # 50 shares of B at cost 10_000, now 5_000 → loss 250k total (from B alone)
        agent.holdings["B"] = 50
        agent.last_trade["B"] = 5_000
        # CASH = starting - 50*10000 - 50*10000 = 10M - 1M = 9M
        agent.holdings["CASH"] = 10_000_000 - 50 * 10_000 - 50 * 10_000
        # mark = 9_000_000 + 50*8000 + 50*5000 = 9_000_000 + 400_000 + 250_000 = 9_650_000
        # loss = 10_000_000 - 9_650_000 = 350_000 >= 300_000
        assert agent._check_circuit_breaker() is True


class TestOrderRateWindowBoundary:
    """Tumbling window edge cases for order-rate circuit breaker."""

    def test_window_expires_resets_counter(self):
        """After window_ns passes, counter resets and breaker does not trip."""
        agent = _make_agent(max_order_rate=2, order_rate_window_ns=1_000_000_000)
        # First window: 2 orders
        agent._window_start = MKT_OPEN
        agent._order_count_in_window = 2
        # Advance time past window.
        agent.current_time = MKT_OPEN + 1_000_000_001
        # call _record_order_for_rate_check to trigger reset
        agent._record_order_for_rate_check()
        assert agent._order_count_in_window == 1  # new window, first order
        assert agent._check_circuit_breaker() is False

    def test_exactly_at_window_boundary_resets(self):
        """current_time - window_start == window_ns → new window."""
        agent = _make_agent(max_order_rate=2, order_rate_window_ns=1_000_000_000)
        agent._window_start = MKT_OPEN
        agent._order_count_in_window = 2
        agent.current_time = MKT_OPEN + 1_000_000_000  # exactly == window_ns
        agent._record_order_for_rate_check()
        assert agent._order_count_in_window == 1  # reset occurred

    def test_one_ns_before_window_expires(self):
        """current_time - window_start == window_ns - 1 → still in same window."""
        agent = _make_agent(max_order_rate=2, order_rate_window_ns=1_000_000_000)
        agent._window_start = MKT_OPEN
        agent._order_count_in_window = 1
        agent.current_time = MKT_OPEN + 999_999_999
        agent._record_order_for_rate_check()
        assert agent._order_count_in_window == 2  # same window, incremented


class TestBreakerLatchWithPositionLimit:
    """Once breaker latches, position limit becomes irrelevant."""

    def test_latch_persists_through_position_recovery(self):
        """Even if holdings return within limit, breaker stays tripped."""
        agent = _make_agent(position_limit=100, max_drawdown=100_000)
        # Trip via drawdown.
        agent.holdings[SYMBOL] = 100
        agent.holdings["CASH"] = 10_000_000 - 100 * 10_000
        agent.last_trade[SYMBOL] = 8_000  # loss=200k > 100k
        agent._check_circuit_breaker()
        assert agent._circuit_breaker_tripped is True

        # "Recover" the loss — price goes back up.
        agent.last_trade[SYMBOL] = 12_000
        # Breaker is latched: still True.
        assert agent._check_circuit_breaker() is True

        # Even a perfectly safe order is blocked.
        agent.holdings[SYMBOL] = 0
        result = agent.create_limit_order(SYMBOL, 1, Side.BID, 10_000)
        assert result is None

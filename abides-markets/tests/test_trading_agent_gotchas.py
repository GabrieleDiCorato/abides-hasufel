"""Tests for TradingAgent async data-flow gotchas and edge cases.

Covers the documented traps from llm-gotchas.md (docs/reference/):
- mark_to_market() with missing last_trade, asymmetric quotes, default basket_size/nav_diff
- _pending_order_delta() with malformed orders (missing symbol attr)
- get_known_bid_ask() before any spread response
- wakeup() before MarketHoursMsg and after market close
- Holdings tracking after fills
"""

from __future__ import annotations

import numpy as np

from abides_core.utils import datetime_str_to_ns, str_to_ns
from abides_markets.agents.trading_agent import TradingAgent
from abides_markets.messages.orderbook import OrderRejectedMsg, RejectReason
from abides_markets.orders import LimitOrder, Side

DATE = datetime_str_to_ns("20210205")
MKT_OPEN = DATE + str_to_ns("09:30:00")
MKT_CLOSE = MKT_OPEN + str_to_ns("06:30:00")
SYMBOL = "TEST"


def _make_agent(**kwargs) -> TradingAgent:
    defaults = dict(
        id=0,
        random_state=np.random.RandomState(42),
        starting_cash=10_000_000,
    )
    merged = {**defaults, **kwargs}
    agent = TradingAgent(**merged)
    agent.exchange_id = 99
    agent.current_time = MKT_OPEN
    agent.mkt_open = MKT_OPEN
    agent.mkt_close = MKT_CLOSE
    agent.kernel = object()  # type: ignore[assignment]  # stub — Agent.wakeup() asserts kernel is not None
    agent.send_message = lambda *a, **kw: None  # type: ignore[method-assign]
    agent.send_message_batch = lambda *a, **kw: None  # type: ignore[method-assign]
    agent.logEvent = lambda *a, **kw: None  # type: ignore[method-assign]
    return agent


# ===================================================================
# mark_to_market edge cases
# ===================================================================


class TestMarkToMarket:
    """Edge cases for mark_to_market()."""

    def test_fresh_agent_cash_only(self):
        """Agent with no positions → returns starting cash."""
        agent = _make_agent(starting_cash=5_000_000)
        mtm = agent.mark_to_market(agent.holdings)
        assert mtm == 5_000_000

    def test_missing_last_trade_values_position_at_zero(self):
        """Agent holds shares but no last_trade for symbol → position valued at 0."""
        agent = _make_agent(starting_cash=5_000_000)
        agent.holdings[SYMBOL] = 100  # 100 shares, no price info
        mtm = agent.mark_to_market(agent.holdings)
        # Position should be valued at 0 (no price available)
        assert mtm == 5_000_000

    def test_with_last_trade(self):
        """Agent holds shares and last_trade is known → correct mark."""
        agent = _make_agent(starting_cash=5_000_000)
        agent.holdings[SYMBOL] = 100
        agent.last_trade[SYMBOL] = 10_000  # $100.00 per share
        mtm = agent.mark_to_market(agent.holdings)
        assert mtm == 5_000_000 + 100 * 10_000

    def test_negative_position_with_last_trade(self):
        """Short position with last_trade → correct negative mark."""
        agent = _make_agent(starting_cash=5_000_000)
        agent.holdings[SYMBOL] = -50
        agent.last_trade[SYMBOL] = 10_000
        mtm = agent.mark_to_market(agent.holdings)
        assert mtm == 5_000_000 - 50 * 10_000

    def test_default_basket_size_and_nav_diff(self):
        """basket_size=0 and nav_diff=0 by default → no adjustment to cash component."""
        agent = _make_agent()
        assert agent.basket_size == 0
        assert agent.nav_diff == 0
        # Should not affect mark_to_market
        mtm = agent.mark_to_market(agent.holdings)
        assert mtm == agent.starting_cash

    def test_basket_size_nav_diff_adjustment(self):
        """Non-zero basket_size × nav_diff adds to cash component."""
        agent = _make_agent(starting_cash=5_000_000)
        agent.basket_size = 10
        agent.nav_diff = 500  # 10 * 500 = 5000 cents
        mtm = agent.mark_to_market(agent.holdings)
        assert mtm == 5_000_000 + 10 * 500

    def test_multiple_symbols_mixed_prices(self):
        """Holdings in two symbols, one with price, one without."""
        agent = _make_agent(starting_cash=1_000_000)
        agent.holdings["AAPL"] = 10
        agent.holdings["MSFT"] = 20
        agent.last_trade["AAPL"] = 15_000  # $150.00
        # MSFT has no last_trade → valued at 0
        mtm = agent.mark_to_market(agent.holdings)
        assert mtm == 1_000_000 + 10 * 15_000 + 0


# ===================================================================
# _pending_order_delta edge cases
# ===================================================================


class TestPendingOrderDelta:
    """Edge cases for _pending_order_delta()."""

    def test_order_missing_symbol_attr_is_skipped(self):
        """An order without a symbol attribute is safely skipped via getattr."""
        agent = _make_agent()

        # Create a mock order with no symbol attribute
        class BrokenOrder:
            order_id = 999
            quantity = 100
            side = Side.BID

        agent.orders[999] = BrokenOrder()

        # Should not raise, should return 0 (broken order is skipped)
        delta = agent._pending_order_delta(SYMBOL)
        assert delta == 0

    def test_mixed_buy_sell_orders(self):
        """Multiple buy and sell orders → net delta."""
        agent = _make_agent()

        buy1 = LimitOrder(0, MKT_OPEN, SYMBOL, 100, Side.BID, 10_000)
        buy2 = LimitOrder(0, MKT_OPEN, SYMBOL, 50, Side.BID, 10_000)
        sell1 = LimitOrder(0, MKT_OPEN, SYMBOL, 30, Side.ASK, 10_000)

        agent.orders[buy1.order_id] = buy1
        agent.orders[buy2.order_id] = buy2
        agent.orders[sell1.order_id] = sell1

        delta = agent._pending_order_delta(SYMBOL)
        assert delta == 100 + 50 - 30  # 120

    def test_exclude_order_id(self):
        """Excluding an order_id removes it from the delta calculation."""
        agent = _make_agent()

        buy = LimitOrder(0, MKT_OPEN, SYMBOL, 100, Side.BID, 10_000)
        sell = LimitOrder(0, MKT_OPEN, SYMBOL, 50, Side.ASK, 10_000)

        agent.orders[buy.order_id] = buy
        agent.orders[sell.order_id] = sell

        # Without exclusion
        delta_full = agent._pending_order_delta(SYMBOL)
        assert delta_full == 100 - 50

        # Exclude the buy
        delta_excl = agent._pending_order_delta(SYMBOL, exclude_order_id=buy.order_id)
        assert delta_excl == -50

    def test_wrong_symbol_orders_ignored(self):
        """Orders for a different symbol not counted."""
        agent = _make_agent()

        other = LimitOrder(0, MKT_OPEN, "OTHER", 200, Side.BID, 10_000)
        agent.orders[other.order_id] = other

        delta = agent._pending_order_delta(SYMBOL)
        assert delta == 0


# ===================================================================
# get_known_bid_ask before any data
# ===================================================================


class TestGetKnownBidAskBeforeData:
    """Test get_known_bid_ask() when no spread response has been received."""

    def test_unknown_symbol_returns_none_zero(self):
        """Before any spread response → (None, 0, None, 0)."""
        agent = _make_agent()
        bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask(SYMBOL)
        assert bid is None
        assert bid_vol == 0
        assert ask is None
        assert ask_vol == 0

    def test_bid_only_known(self):
        """Only bid side populated → ask is None."""
        agent = _make_agent()
        agent.known_bids[SYMBOL] = [(10_000, 100)]
        bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask(SYMBOL)
        assert bid == 10_000
        assert bid_vol == 100
        assert ask is None
        assert ask_vol == 0

    def test_ask_only_known(self):
        """Only ask side populated → bid is None."""
        agent = _make_agent()
        agent.known_asks[SYMBOL] = [(10_100, 50)]
        bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask(SYMBOL)
        assert bid is None
        assert bid_vol == 0
        assert ask == 10_100
        assert ask_vol == 50

    def test_empty_list_returns_none(self):
        """known_bids[SYMBOL] = [] → bid is None."""
        agent = _make_agent()
        agent.known_bids[SYMBOL] = []
        agent.known_asks[SYMBOL] = []
        bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask(SYMBOL)
        assert bid is None
        assert ask is None

    def test_both_sides_known(self):
        """Both sides populated → returns prices and volumes from both sides."""
        agent = _make_agent()
        agent.known_bids[SYMBOL] = [(10_000, 100)]
        agent.known_asks[SYMBOL] = [(10_100, 50)]
        bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask(SYMBOL)
        assert bid == 10_000
        assert bid_vol == 100
        assert ask == 10_100
        assert ask_vol == 50


# ===================================================================
# wakeup() gating on market hours
# ===================================================================


class TestWakeupGating:
    """Test that wakeup() returns False when market hours unknown or closed."""

    def test_wakeup_before_market_hours_known(self):
        """mkt_open is None → returns falsy (None or False)."""
        agent = _make_agent()
        agent.mkt_open = None
        agent.mkt_close = None
        result = agent.wakeup(MKT_OPEN)
        assert not result

    def test_wakeup_when_market_closed(self):
        """mkt_closed flag set → returns False."""
        agent = _make_agent()
        agent.mkt_closed = True
        result = agent.wakeup(MKT_CLOSE + 1)
        assert result is False

    def test_wakeup_during_market_hours(self):
        """Normal operation → returns True."""
        agent = _make_agent()
        agent.first_wake = False  # skip side-effects
        result = agent.wakeup(MKT_OPEN + str_to_ns("00:05:00"))
        assert result is True


# ===================================================================
# Holdings integrity
# ===================================================================


class TestHoldingsIntegrity:
    def test_cash_always_present(self):
        agent = _make_agent(starting_cash=1_000_000)
        assert "CASH" in agent.holdings
        assert agent.holdings["CASH"] == 1_000_000

    def test_holdings_dict_type(self):
        agent = _make_agent()
        assert isinstance(agent.holdings, dict)
        assert isinstance(agent.holdings["CASH"], int)


# ===================================================================
# get_known_liquidity edge cases
# ===================================================================


class TestGetKnownLiquidity:
    def test_unknown_symbol_returns_zero_liquidity(self):
        """get_known_liquidity returns (0, 0) for an unknown symbol, not KeyError."""
        agent = _make_agent()
        agent.known_bids = {}
        agent.known_asks = {}
        bid_liq, ask_liq = agent.get_known_liquidity("UNKNOWN")
        assert bid_liq == 0
        assert ask_liq == 0


# ===================================================================
# on_order_rejected
# ===================================================================


class TestOnOrderRejected:
    """Tests for the TradingAgent.on_order_rejected() hook."""

    def test_hook_dispatched_via_handle_method(self):
        """_handle_order_rejected_msg dispatches to on_order_rejected."""
        rejected_calls: list = []

        class _TestAgent(TradingAgent):
            def on_order_rejected(self, order_id, reason):
                rejected_calls.append((order_id, reason))

        agent = _TestAgent(id=0, name="test", random_state=np.random.RandomState(42))
        msg = OrderRejectedMsg(order_id=42, reason=RejectReason.INVALID_PRICE)
        agent._handle_order_rejected_msg(msg)

        assert len(rejected_calls) == 1
        oid, reason = rejected_calls[0]
        assert oid == 42
        assert reason is RejectReason.INVALID_PRICE

    def test_default_removes_order_from_self_orders(self):
        """on_order_rejected() removes the rejected order from self.orders."""
        agent = _make_agent()
        fake_order = LimitOrder(
            agent_id=0,
            time_placed=0,
            symbol=SYMBOL,
            quantity=10,
            side=Side.BID,
            limit_price=10_000,
            order_id=99,
        )
        agent.orders[99] = fake_order
        assert 99 in agent.orders
        agent.on_order_rejected(99, RejectReason.INVALID_QUANTITY)
        assert 99 not in agent.orders

    def test_no_logEvent_when_log_orders_false(self):
        """on_order_rejected() does not call logEvent when log_orders is False (default)."""
        logEvent_calls: list = []

        class _TestAgent(TradingAgent):
            def logEvent(self, *args, **kwargs):
                logEvent_calls.append(args)

        agent = _TestAgent(id=0, name="test", random_state=np.random.RandomState(42))
        assert agent.log_orders is False
        agent.on_order_rejected(55, RejectReason.UNKNOWN_SYMBOL)
        assert logEvent_calls == []

    def test_logEvent_emitted_when_log_orders_true(self):
        """on_order_rejected() emits EventType.ORDER_REJECTED when log_orders is True."""
        from abides_core.telemetry.event_payloads import EventType

        logEvent_calls: list = []

        class _TestAgent(TradingAgent):
            def logEvent(self, event_type, payload=None, **kwargs):
                logEvent_calls.append((event_type, payload))

        agent = _TestAgent(id=0, name="test", random_state=np.random.RandomState(42))
        agent.log_orders = True
        agent.orders[77] = object()
        agent.on_order_rejected(77, RejectReason.INVALID_PRICE)

        assert any(et is EventType.ORDER_REJECTED for et, _ in logEvent_calls)
        et, payload = next(
            (et, p) for et, p in logEvent_calls if et is EventType.ORDER_REJECTED
        )
        assert payload == (77, "INVALID_PRICE")

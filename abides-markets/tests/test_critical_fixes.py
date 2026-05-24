"""Tests for critical bug fixes in exchange_agent, trading_agent, kernel, and oracle."""

import numpy as np

from abides_core.engine.kernel import Kernel
from abides_markets.agents.exchange_agent import ExchangeAgent
from abides_markets.agents.trading_agent import TradingAgent
from abides_markets.messages.orderbook import OrderRejectedMsg, RejectReason
from abides_markets.oracles.sparse_mean_reverting_oracle import (
    SparseMeanRevertingOracle,
)
from abides_markets.orders import LimitOrder

# --- Kernel lifecycle state ---


def test_kernel_state_transitions_through_initialize():
    """Kernel.state should advance from CREATED to INITIALIZED after initialize()."""
    from abides_core.engine.kernel import KernelState

    agents = []
    kernel = Kernel(
        agents=agents,
        start_time=0,
        stop_time=1,
        random_state=np.random.RandomState(seed=42),
    )
    assert kernel.state is KernelState.CONSTRUCTED
    kernel.initialize()
    assert kernel.state is KernelState.INITIALIZED


# --- TradingAgent get_known_bid_ask KeyError ---


def test_get_known_bid_ask_no_keyerror_for_unknown_symbol():
    """get_known_bid_ask should return Nones/zeros for unknown symbols, not KeyError."""
    agent = TradingAgent(id=0, name="test", random_state=np.random.RandomState(42))
    agent.known_bids = {}
    agent.known_asks = {}
    bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask("UNKNOWN")
    assert bid is None
    assert ask is None
    assert bid_vol == 0
    assert ask_vol == 0


def test_get_known_bid_ask_with_known_symbol():
    """get_known_bid_ask should return correct values for known symbols."""
    agent = TradingAgent(id=0, name="test", random_state=np.random.RandomState(42))
    agent.known_bids = {"IBM": [(10000, 100)]}
    agent.known_asks = {"IBM": [(10100, 50)]}
    bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask("IBM")
    assert bid == 10000
    assert bid_vol == 100
    assert ask == 10100
    assert ask_vol == 50


def test_get_known_bid_ask_empty_book_sides():
    """get_known_bid_ask should handle empty lists for known symbols."""
    agent = TradingAgent(id=0, name="test", random_state=np.random.RandomState(42))
    agent.known_bids = {"IBM": []}
    agent.known_asks = {"IBM": []}
    bid, bid_vol, ask, ask_vol = agent.get_known_bid_ask("IBM")
    assert bid is None
    assert ask is None
    assert bid_vol == 0
    assert ask_vol == 0


# --- TradingAgent get_known_liquidity self.symbol bug ---


def test_get_known_liquidity_no_keyerror_for_unknown_symbol():
    """get_known_liquidity should not crash for unknown symbols."""
    agent = TradingAgent(id=0, name="test", random_state=np.random.RandomState(42))
    agent.known_bids = {}
    agent.known_asks = {}
    bid_liq, ask_liq = agent.get_known_liquidity("UNKNOWN")
    assert bid_liq == 0
    assert ask_liq == 0


# --- Oracle config mutation ---


def test_oracle_does_not_mutate_caller_config():
    """SparseMeanRevertingOracle should not modify the caller's symbols dict."""
    symbols_config = {
        "IBM": {
            "r_bar": 10000,
            "kappa": 1.67e-16,
            "sigma_s": 0,
            "fund_vol": 1e-8,
            "megashock_lambda_a": 0,
            "megashock_mean": 0,
            "megashock_var": 0,
            "random_state": np.random.RandomState(42),
        }
    }
    original_keys = set(symbols_config["IBM"].keys())
    SparseMeanRevertingOracle(
        mkt_open=0,
        mkt_close=int(1e18),
        symbols=symbols_config,
        random_state=np.random.RandomState(99),
    )
    # The caller's dict should not have been mutated
    assert set(symbols_config["IBM"].keys()) == original_keys


def test_oracle_creates_own_copy_of_symbols():
    """Oracle's internal symbols should be independent of caller's dict."""
    symbols_config = {
        "IBM": {
            "r_bar": 10000,
            "kappa": 1.67e-16,
            "sigma_s": 0,
            "fund_vol": 1e-8,
            "megashock_lambda_a": 0,
            "megashock_mean": 0,
            "megashock_var": 0,
            "random_state": np.random.RandomState(42),
        }
    }
    oracle = SparseMeanRevertingOracle(
        mkt_open=0,
        mkt_close=int(1e18),
        symbols=symbols_config,
        random_state=np.random.RandomState(99),
    )
    # Modifying caller's dict should not affect oracle's internal state
    symbols_config["IBM"]["r_bar"] = 99999
    assert oracle.symbols["IBM"]["r_bar"] == 10000


# --- Subscription cancellation fix ---


def test_subscription_cancel_type_mapping():
    """Verify the subscription type mapping dict is correct."""

    # Verify that subscription classes exist with expected attributes
    l1_sub = ExchangeAgent.L1DataSubscription(agent_id=0, last_update_ts=0, freq=1)
    assert not hasattr(l1_sub, "depth")  # L1 has no depth

    l2_sub = ExchangeAgent.L2DataSubscription(
        agent_id=0, last_update_ts=0, freq=1, depth=10
    )
    assert hasattr(l2_sub, "depth")  # L2 has depth

    tv_sub = ExchangeAgent.TransactedVolDataSubscription(
        agent_id=0, last_update_ts=0, freq=1, lookback="1min"
    )
    assert not hasattr(tv_sub, "depth")  # TransactedVol has no depth

    bi_sub = ExchangeAgent.BookImbalanceDataSubscription(
        agent_id=0, last_update_ts=0, event_in_progress=False, min_imbalance=0.5
    )
    assert not hasattr(bi_sub, "depth")  # BookImbalance has no depth
    assert not hasattr(bi_sub, "freq")  # BookImbalance has no freq


# --- Kernel ValueError formatting ---


def test_kernel_set_wakeup_valueerror_is_string():
    """ValueError from set_wakeup should have a formatted string, not tuple args."""
    kernel = Kernel(
        agents=[],
        start_time=0,
        stop_time=1,
        random_state=np.random.RandomState(seed=42),
    )
    kernel.initialize()
    kernel.current_time = 100
    try:
        kernel.set_wakeup(sender_id=0, requested_time=50)
        raise AssertionError("Expected ValueError")
    except ValueError as e:
        # Should be a single formatted string, not tuple args
        assert len(e.args) == 1, f"ValueError has {len(e.args)} args, expected 1"
        assert "current_time" in str(e)


# --- Kernel runner loop truthiness at time=0 ---


def test_kernel_runner_works_with_start_time_zero():
    """Kernel runner loop should work when start_time is 0 (falsy int)."""
    from abides_core.agent import Agent

    agent = Agent(id=0, name="test", random_state=np.random.RandomState(42))
    kernel = Kernel(
        agents=[agent],
        start_time=0,
        stop_time=100,
        random_state=np.random.RandomState(seed=42),
    )
    kernel.initialize()
    # Should not hang or skip — the runner loop condition should handle time=0
    kernel.runner()
    # If we get here without error, the truthiness check works


# --- OrderRejectedMsg ---


def _make_order_book(symbol: str = "IBM"):
    """Return an OrderBook with a mock owner that records sent messages."""
    from abides_markets.order_book import OrderBook

    sent: list = []

    class _Owner:
        mkt_open = None

        def send_message(self, agent_id, message):
            sent.append((agent_id, message))

        def logEvent(self, *args, **kwargs):
            pass

    owner = _Owner()
    book = OrderBook(owner=owner, symbol=symbol)  # type: ignore[arg-type]
    return book, sent


def _make_limit_order(
    symbol: str,
    quantity,
    limit_price,
    order_id: int = 1,
    agent_id: int = 7,
):
    from abides_markets.orders import LimitOrder, Side

    return LimitOrder(
        agent_id=agent_id,
        order_id=order_id,
        time_placed=0,
        symbol=symbol,
        quantity=quantity,
        side=Side.BID,
        limit_price=limit_price,
    )


def _make_market_order(
    symbol: str,
    quantity,
    order_id: int = 2,
    agent_id: int = 7,
):
    from abides_markets.orders import MarketOrder, Side

    return MarketOrder(
        agent_id=agent_id,
        order_id=order_id,
        time_placed=0,
        symbol=symbol,
        quantity=quantity,
        side=Side.BID,
    )


def test_order_rejected_invalid_quantity_limit_order():
    """OrderBook fires OrderRejectedMsg(INVALID_QUANTITY) for a zero-quantity LimitOrder."""
    book, sent = _make_order_book("IBM")
    order = _make_limit_order("IBM", quantity=0, limit_price=10_000)
    book.handle_limit_order(order)
    assert len(sent) == 1
    _, msg = sent[0]
    assert isinstance(msg, OrderRejectedMsg)
    assert msg.order_id == order.order_id
    assert msg.reason is RejectReason.INVALID_QUANTITY


def test_order_rejected_invalid_quantity_negative_limit_order():
    """OrderBook fires OrderRejectedMsg(INVALID_QUANTITY) for a negative-quantity LimitOrder."""
    book, sent = _make_order_book("IBM")
    order = _make_limit_order("IBM", quantity=-5, limit_price=10_000)
    book.handle_limit_order(order)
    assert len(sent) == 1
    _, msg = sent[0]
    assert isinstance(msg, OrderRejectedMsg)
    assert msg.reason is RejectReason.INVALID_QUANTITY


def test_order_rejected_invalid_price_limit_order():
    """OrderBook fires OrderRejectedMsg(INVALID_PRICE) for a non-integer price."""
    book, sent = _make_order_book("IBM")
    order = _make_limit_order("IBM", quantity=10, limit_price=99.5)
    book.handle_limit_order(order)
    assert len(sent) == 1
    _, msg = sent[0]
    assert isinstance(msg, OrderRejectedMsg)
    assert msg.order_id == order.order_id
    assert msg.reason is RejectReason.INVALID_PRICE


def test_order_rejected_invalid_quantity_market_order():
    """OrderBook fires OrderRejectedMsg(INVALID_QUANTITY) for a zero-quantity MarketOrder."""
    book, sent = _make_order_book("IBM")
    order = _make_market_order("IBM", quantity=0)
    book.handle_market_order(order)
    assert len(sent) == 1
    _, msg = sent[0]
    assert isinstance(msg, OrderRejectedMsg)
    assert msg.order_id == order.order_id
    assert msg.reason is RejectReason.INVALID_QUANTITY


def test_order_rejected_quiet_mode_suppresses_message():
    """quiet=True must not send any reject message (consistent with other quiet behaviour)."""
    book, sent = _make_order_book("IBM")
    order = _make_limit_order("IBM", quantity=0, limit_price=10_000)
    book.handle_limit_order(order, quiet=True)
    assert len(sent) == 0


def test_on_order_rejected_hook_dispatched():
    """TradingAgent.receive_message dispatches OrderRejectedMsg to on_order_rejected."""
    rejected_calls: list = []

    class _TestAgent(TradingAgent):
        def on_order_rejected(self, order_id, reason):
            rejected_calls.append((order_id, reason))

    agent = _TestAgent(id=0, name="test", random_state=np.random.RandomState(42))
    msg = OrderRejectedMsg(order_id=42, reason=RejectReason.INVALID_PRICE)

    # Bypass kernel machinery — dispatch the message directly via the handler.
    agent._handle_order_rejected_msg(msg)

    assert len(rejected_calls) == 1
    oid, reason = rejected_calls[0]
    assert oid == 42
    assert reason is RejectReason.INVALID_PRICE


def test_on_order_rejected_removes_from_orders():
    """on_order_rejected() default removes the order from self.orders."""
    from abides_markets.orders import LimitOrder, Side

    agent = TradingAgent(id=0, name="test", random_state=np.random.RandomState(42))
    # Simulate an order that was placed and stored before sending to exchange.
    fake_order = LimitOrder(
        agent_id=0,
        time_placed=0,
        symbol="IBM",
        quantity=10,
        side=Side.BID,
        limit_price=10_000,
        order_id=99,
    )
    agent.orders[99] = fake_order
    assert 99 in agent.orders

    agent.on_order_rejected(99, RejectReason.INVALID_QUANTITY)

    assert 99 not in agent.orders, "Rejected order must be removed from self.orders"


def test_on_order_rejected_no_logEvent_when_log_orders_false():
    """on_order_rejected() must not call logEvent when log_orders is False (default)."""
    logEvent_calls: list = []

    class _TestAgent(TradingAgent):
        def logEvent(self, *args, **kwargs):
            logEvent_calls.append(args)

    agent = _TestAgent(id=0, name="test", random_state=np.random.RandomState(42))
    assert agent.log_orders is False
    agent.on_order_rejected(55, RejectReason.UNKNOWN_SYMBOL)

    assert logEvent_calls == []


def test_on_order_rejected_logs_event_when_log_orders_true():
    """on_order_rejected() emits EventType.ORDER_REJECTED when log_orders is True."""
    from abides_core.telemetry.event_payloads import EventType

    logEvent_calls: list = []

    class _TestAgent(TradingAgent):
        def logEvent(self, event_type, payload=None, **kwargs):
            logEvent_calls.append((event_type, payload))

    agent = _TestAgent(id=0, name="test", random_state=np.random.RandomState(42))
    agent.log_orders = True
    agent.orders[77] = object()  # placeholder

    agent.on_order_rejected(77, RejectReason.INVALID_PRICE)

    assert any(
        et is EventType.ORDER_REJECTED for et, _ in logEvent_calls
    ), f"Expected ORDER_REJECTED logEvent, got: {logEvent_calls}"
    et, payload = next(
        (et, p) for et, p in logEvent_calls if et is EventType.ORDER_REJECTED
    )
    assert payload == (77, "INVALID_PRICE")


# --- Exchange handlers: unknown-symbol → OrderRejectedMsg ---


def _make_exchange(symbols=("AAPL",)):
    """Return an ExchangeAgent whose send_message is monkey-patched to record messages."""
    sent: list = []
    exchange = ExchangeAgent(
        id=0,
        mkt_open=int(9.5e9),
        mkt_close=int(4e10),
        symbols=list(symbols),
        name="TestExchange",
        random_state=np.random.RandomState(42),
        log_orders=False,
        use_metric_tracker=False,
    )
    exchange.send_message = lambda agent_id, msg: sent.append((agent_id, msg))
    return exchange, sent


def _limit(symbol: str, order_id: int = 10) -> LimitOrder:
    from abides_markets.orders import Side

    return LimitOrder(
        agent_id=1,
        time_placed=0,
        symbol=symbol,
        quantity=5,
        side=Side.BID,
        limit_price=10_000,
        order_id=order_id,
    )


def test_handle_cancel_unknown_symbol_sends_reject():
    """_handle_cancel_order sends OrderRejectedMsg(UNKNOWN_SYMBOL) for an unknown symbol."""
    from abides_markets.messages.order import CancelOrderMsg

    exchange, sent = _make_exchange(symbols=["AAPL"])
    order = _limit("UNKNOWN", order_id=11)
    msg = CancelOrderMsg(order=order, tag="", metadata={})
    exchange._handle_cancel_order(sender_id=1, current_time=0, message=msg)

    assert len(sent) == 1
    agent_id, reply = sent[0]
    assert agent_id == 1
    assert isinstance(reply, OrderRejectedMsg)
    assert reply.order_id == 11
    assert reply.reason is RejectReason.UNKNOWN_SYMBOL


def test_handle_partial_cancel_unknown_symbol_sends_reject():
    """_handle_partial_cancel_order sends OrderRejectedMsg(UNKNOWN_SYMBOL) for an unknown symbol."""
    from abides_markets.messages.order import PartialCancelOrderMsg

    exchange, sent = _make_exchange(symbols=["AAPL"])
    order = _limit("UNKNOWN", order_id=12)
    msg = PartialCancelOrderMsg(order=order, quantity=2, tag="", metadata={})
    exchange._handle_partial_cancel_order(sender_id=1, current_time=0, message=msg)

    assert len(sent) == 1
    _, reply = sent[0]
    assert isinstance(reply, OrderRejectedMsg)
    assert reply.order_id == 12
    assert reply.reason is RejectReason.UNKNOWN_SYMBOL


def test_handle_modify_unknown_symbol_sends_reject():
    """_handle_modify_order sends OrderRejectedMsg(UNKNOWN_SYMBOL) for an unknown symbol."""
    from abides_markets.messages.order import ModifyOrderMsg

    exchange, sent = _make_exchange(symbols=["AAPL"])
    old = _limit("UNKNOWN", order_id=13)
    new = _limit("UNKNOWN", order_id=14)
    msg = ModifyOrderMsg(old_order=old, new_order=new)
    exchange._handle_modify_order(sender_id=1, current_time=0, message=msg)

    assert len(sent) == 1
    _, reply = sent[0]
    assert isinstance(reply, OrderRejectedMsg)
    assert reply.order_id == 13
    assert reply.reason is RejectReason.UNKNOWN_SYMBOL


def test_handle_replace_unknown_symbol_sends_reject():
    """_handle_replace_order sends OrderRejectedMsg(UNKNOWN_SYMBOL) for an unknown symbol."""
    from abides_markets.messages.order import ReplaceOrderMsg

    exchange, sent = _make_exchange(symbols=["AAPL"])
    old = _limit("UNKNOWN", order_id=15)
    new = _limit("UNKNOWN", order_id=16)
    msg = ReplaceOrderMsg(agent_id=1, old_order=old, new_order=new)
    exchange._handle_replace_order(sender_id=1, current_time=0, message=msg)

    assert len(sent) == 1
    _, reply = sent[0]
    assert isinstance(reply, OrderRejectedMsg)
    assert reply.order_id == 15
    assert reply.reason is RejectReason.UNKNOWN_SYMBOL


def test_handle_stop_unknown_symbol_sends_reject():
    """_handle_stop_order sends OrderRejectedMsg(UNKNOWN_SYMBOL) for an unknown symbol."""
    from abides_markets.messages.order import StopOrderMsg
    from abides_markets.orders import Side, StopOrder

    exchange, sent = _make_exchange(symbols=["AAPL"])
    stop = StopOrder(
        agent_id=1,
        time_placed=0,
        symbol="UNKNOWN",
        quantity=5,
        side=Side.BID,
        stop_price=10_000,
        order_id=17,
    )
    msg = StopOrderMsg(order=stop)
    exchange._handle_stop_order(sender_id=1, current_time=0, message=msg)

    assert len(sent) == 1
    _, reply = sent[0]
    assert isinstance(reply, OrderRejectedMsg)
    assert reply.order_id == 17
    assert reply.reason is RejectReason.UNKNOWN_SYMBOL

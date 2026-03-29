import pytest

from abides_markets.messages.orderbook import OrderExecutedMsg
from abides_markets.order_book import OrderBook
from abides_markets.orders import LimitOrder, Side
from abides_markets.price_level import PriceLevel

from . import SYMBOL, TIME, FakeExchangeAgent, setup_book_with_orders


def test_handle_limit_orders():
    # Test insert on bid side
    bid_order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.BID,
        limit_price=100,
    )

    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)
    book.handle_limit_order(bid_order)

    assert book.bids == [PriceLevel([(bid_order, {})])]
    assert book.asks == []

    assert len(agent.messages) == 1
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.limit_price == 100
    assert agent.messages[0][1].order.quantity == 10

    # Test insert on ask side
    ask_order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.ASK,
        limit_price=100,
    )

    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)
    book.handle_limit_order(ask_order)

    assert book.bids == []
    assert book.asks == [PriceLevel([(ask_order, {})])]

    assert len(agent.messages) == 1
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.limit_price == 100
    assert agent.messages[0][1].order.quantity == 10


def test_handle_hidden_limit_orders():
    # Test insert on bid side
    bid_order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.BID,
        is_hidden=True,
        limit_price=100,
    )

    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)
    book.handle_limit_order(bid_order)

    assert book.bids == [PriceLevel([(bid_order, {})])]
    assert book.asks == []

    assert len(agent.messages) == 1
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.is_hidden
    assert agent.messages[0][1].order.limit_price == 100
    assert agent.messages[0][1].order.quantity == 10

    # Test insert on ask side
    ask_order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.ASK,
        is_hidden=True,
        limit_price=100,
    )

    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)
    book.handle_limit_order(ask_order)

    assert book.bids == []
    assert book.asks == [PriceLevel([(ask_order, {})])]

    assert len(agent.messages) == 1
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.is_hidden
    assert agent.messages[0][1].order.limit_price == 100
    assert agent.messages[0][1].order.quantity == 10


def test_handle_matching_limit_orders():
    # Test insert on bid side
    book, agent, _ = setup_book_with_orders(
        asks=[
            (100, [30]),
        ],
    )

    bid_order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=30,
        side=Side.BID,
        is_hidden=False,
        limit_price=110,
    )

    book.handle_limit_order(bid_order)

    assert book.bids == []
    assert book.asks == []

    assert len(agent.messages) == 2

    assert agent.messages[0][0] == 1
    assert isinstance(agent.messages[0][1], OrderExecutedMsg)
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.limit_price == 100
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30

    assert agent.messages[1][0] == 1
    assert isinstance(agent.messages[1][1], OrderExecutedMsg)
    assert agent.messages[1][1].order.agent_id == 1
    assert agent.messages[1][1].order.side == Side.BID
    assert agent.messages[1][1].order.limit_price == 110
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30

    # Test insert on ask side
    book, agent, _ = setup_book_with_orders(
        bids=[
            (100, [30]),
        ],
    )

    ask_order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=30,
        side=Side.ASK,
        is_hidden=False,
        limit_price=90,
    )

    book.handle_limit_order(ask_order)

    assert book.bids == []
    assert book.asks == []

    assert len(agent.messages) == 2

    assert agent.messages[0][0] == 1
    assert isinstance(agent.messages[0][1], OrderExecutedMsg)
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.limit_price == 100
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30

    assert agent.messages[1][0] == 1
    assert isinstance(agent.messages[1][1], OrderExecutedMsg)
    assert agent.messages[1][1].order.agent_id == 1
    assert agent.messages[1][1].order.side == Side.ASK
    assert agent.messages[1][1].order.limit_price == 90
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30


def test_handle_bad_limit_orders():
    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)

    # Symbol does not match book
    order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol="BAD",
        quantity=10,
        side=Side.BID,
        is_hidden=True,
        limit_price=100,
    )

    with pytest.warns(UserWarning):
        book.handle_limit_order(order)

    # Order quantity not integer
    order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=1.5,
        side=Side.BID,
        is_hidden=True,
        limit_price=100,
    )

    with pytest.warns(UserWarning):
        book.handle_limit_order(order)

    # Order quantity is negative
    order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=-10,
        side=Side.BID,
        is_hidden=True,
        limit_price=100,
    )

    with pytest.warns(UserWarning):
        book.handle_limit_order(order)

    with pytest.warns(UserWarning):
        book.handle_limit_order(order)

    # Order limit price is negative
    order = LimitOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.BID,
        is_hidden=True,
        limit_price=-100,
    )

    with pytest.warns(UserWarning):
        book.handle_limit_order(order)


def test_handle_insert_by_id_limit_order():
    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)

    order1 = LimitOrder(
        order_id=1,
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.BID,
        limit_price=100,
    )

    order2 = LimitOrder(
        order_id=2,
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=20,
        side=Side.BID,
        limit_price=100,
    )

    order3 = LimitOrder(
        order_id=3,
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=30,
        side=Side.BID,
        limit_price=100,
        insert_by_id=True,
    )

    order4 = LimitOrder(
        order_id=4,
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=40,
        side=Side.BID,
        limit_price=100,
    )

    book.handle_limit_order(order1)
    book.handle_limit_order(order2)
    book.handle_limit_order(order4)

    # Insert out-of-order
    book.handle_limit_order(order3)

    assert book.bids[0].visible_orders == [
        (order1, {}),
        (order2, {}),
        (order3, {}),
        (order4, {}),
    ]


# ===================================================================
# Hidden order priority — visible consumed before hidden at same price
# ===================================================================


def test_hidden_orders_consumed_after_visible_at_same_price():
    """Place 3 visible + 2 hidden orders at the same ask price.

    An incoming bid that consumes 4 should exhaust all 3 visible first,
    then consume 1 hidden. The second hidden order should remain.
    """
    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)

    # 3 visible asks of qty 10 each at price 100
    v1 = LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, 100)
    v2 = LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, 100)
    v3 = LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, 100)
    book.handle_limit_order(v1)
    book.handle_limit_order(v2)
    book.handle_limit_order(v3)

    # 2 hidden asks of qty 10 each at price 100
    h1 = LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, 100, is_hidden=True)
    h2 = LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, 100, is_hidden=True)
    book.handle_limit_order(h1)
    book.handle_limit_order(h2)

    agent.reset()

    # All 5 orders at the same PriceLevel
    assert len(book.asks) == 1
    assert len(book.asks[0].visible_orders) == 3
    assert len(book.asks[0].hidden_orders) == 2

    # Incoming bid for 35 @ 100 → consumes 3 visible (30) + partial hidden (5)
    buyer = LimitOrder(2, TIME, SYMBOL, 35, Side.BID, 100)
    book.handle_limit_order(buyer)

    # Book should still have 1 price level on asks with just the remaining hidden qty
    assert len(book.asks) == 1
    level = book.asks[0]
    assert level.price == 100
    # All visible orders consumed
    assert len(level.visible_orders) == 0
    # First hidden partially consumed (10 → 5), second untouched (10)
    assert len(level.hidden_orders) == 2
    assert level.hidden_orders[0][0].quantity == 5
    assert level.hidden_orders[1][0].quantity == 10


def test_hidden_orders_at_different_prices():
    """Hidden and visible orders at different price levels.

    Ensures hidden orders at a better price level are still consumed
    before visible orders at a worse price level.
    """
    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)

    # Visible ask at 101
    v = LimitOrder(1, TIME, SYMBOL, 50, Side.ASK, 101)
    book.handle_limit_order(v)

    # Hidden ask at 100 (better price for buyer)
    h = LimitOrder(1, TIME, SYMBOL, 50, Side.ASK, 100, is_hidden=True)
    book.handle_limit_order(h)

    agent.reset()

    # Incoming bid for 30 @ 101 → should match hidden at 100 first (best ask)
    buyer = LimitOrder(2, TIME, SYMBOL, 30, Side.BID, 101)
    book.handle_limit_order(buyer)

    # The hidden order at 100 should be partially consumed
    assert len(book.asks) == 2
    assert book.asks[0].price == 100
    assert book.asks[0].hidden_orders[0][0].quantity == 20  # 50 - 30


def test_all_visible_then_hidden_fills_to_completion():
    """A large order that consumes all visible and all hidden at a level."""
    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)

    # 2 visible (20 each) + 1 hidden (10) at price 100 = 50 total
    book.handle_limit_order(LimitOrder(1, TIME, SYMBOL, 20, Side.ASK, 100))
    book.handle_limit_order(LimitOrder(1, TIME, SYMBOL, 20, Side.ASK, 100))
    book.handle_limit_order(
        LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, 100, is_hidden=True)
    )
    agent.reset()

    # Buy exactly 50 → level fully consumed
    buyer = LimitOrder(2, TIME, SYMBOL, 50, Side.BID, 100)
    book.handle_limit_order(buyer)

    assert book.asks == []
    assert book.bids == []

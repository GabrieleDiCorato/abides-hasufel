from abides_markets.messages.orderbook import OrderRejectedMsg, RejectReason
from abides_markets.orders import MarketOrder, Side

from . import SYMBOL, TIME, FakeExchangeAgent, OrderBook, setup_book_with_orders

# fmt: off


def test_handle_market_order_bid_1():
    """Test buy order that partially consumes one order"""

    book, agent, limit_orders = setup_book_with_orders(
        asks=[
            (100, [30]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.BID,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_ask_data() == [
        (100, [20]),
    ]

    assert len(agent.messages) == 2
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 10
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.BID
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 10


def test_handle_market_order_bid_2():
    """Test buy order that fully consumes one order"""

    book, agent, limit_orders = setup_book_with_orders(
        asks=[
            (100, [30]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=30,
        side=Side.BID,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_ask_data() == []

    assert len(agent.messages) == 2
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.BID
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30


def test_handle_market_order_bid_3():
    """Test buy order that consumes multiple orders"""

    book, agent, limit_orders = setup_book_with_orders(
        asks=[
            (100, [30, 40]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=70,
        side=Side.BID,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_ask_data() == []

    assert len(agent.messages) == 4
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.BID
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30
    assert agent.messages[2][0] == 1
    assert agent.messages[2][1].order.agent_id == 1
    assert agent.messages[2][1].order.side == Side.ASK
    assert agent.messages[2][1].order.fill_price == 100
    assert agent.messages[2][1].order.quantity == 40
    assert agent.messages[3][0] == 2
    assert agent.messages[3][1].order.agent_id == 2
    assert agent.messages[3][1].order.side == Side.BID
    assert agent.messages[3][1].order.fill_price == 100
    assert agent.messages[3][1].order.quantity == 40


def test_handle_market_order_bid_4():
    """Test buy order that consumes multiple orders at different prices"""

    book, agent, limit_orders = setup_book_with_orders(
        asks=[
            (100, [30]),
            (200, [40])
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=70,
        side=Side.BID,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_ask_data() == []

    assert len(agent.messages) == 4
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.ASK
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.BID
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30
    assert agent.messages[2][0] == 1
    assert agent.messages[2][1].order.agent_id == 1
    assert agent.messages[2][1].order.side == Side.ASK
    assert agent.messages[2][1].order.fill_price == 200
    assert agent.messages[2][1].order.quantity == 40
    assert agent.messages[3][0] == 2
    assert agent.messages[3][1].order.agent_id == 2
    assert agent.messages[3][1].order.side == Side.BID
    assert agent.messages[3][1].order.fill_price == 200
    assert agent.messages[3][1].order.quantity == 40

def test_handle_market_order_ask_1():
    """Test sell order that partially consumes one order"""

    book, agent, limit_orders = setup_book_with_orders(
        bids=[
            (100, [30]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=10,
        side=Side.ASK,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_bid_data() == [
        (100, [20]),
    ]

    assert len(agent.messages) == 2
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 10
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.ASK
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 10


def test_handle_market_order_ask_2():
    """Test sell order that fully consumes one order"""

    book, agent, limit_orders = setup_book_with_orders(
        bids=[
            (100, [30]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=30,
        side=Side.ASK,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_bid_data() == []

    assert len(agent.messages) == 2
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.ASK
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30


def test_handle_market_order_ask_3():
    """Test sell order that consumes multiple orders"""

    book, agent, limit_orders = setup_book_with_orders(
        bids=[
            (100, [30, 40]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=70,
        side=Side.ASK,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_bid_data() == []

    assert len(agent.messages) == 4
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.fill_price == 100
    assert agent.messages[0][1].order.quantity == 30
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.ASK
    assert agent.messages[1][1].order.fill_price == 100
    assert agent.messages[1][1].order.quantity == 30
    assert agent.messages[2][0] == 1
    assert agent.messages[2][1].order.agent_id == 1
    assert agent.messages[2][1].order.side == Side.BID
    assert agent.messages[2][1].order.fill_price == 100
    assert agent.messages[2][1].order.quantity == 40
    assert agent.messages[3][0] == 2
    assert agent.messages[3][1].order.agent_id == 2
    assert agent.messages[3][1].order.side == Side.ASK
    assert agent.messages[3][1].order.fill_price == 100
    assert agent.messages[3][1].order.quantity == 40


def test_handle_market_order_ask_4():
    """Test sell order that consumes multiple orders at different prices"""

    book, agent, limit_orders = setup_book_with_orders(
        bids=[
            (200, [40]),
            (100, [30]),
        ],
    )

    market_order = MarketOrder(
        agent_id=2,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=70,
        side=Side.ASK,
    )

    book.handle_market_order(market_order)

    assert book.get_l3_bid_data() == []

    assert len(agent.messages) == 4
    assert agent.messages[0][0] == 1
    assert agent.messages[0][1].order.agent_id == 1
    assert agent.messages[0][1].order.side == Side.BID
    assert agent.messages[0][1].order.fill_price == 200
    assert agent.messages[0][1].order.quantity == 40
    assert agent.messages[1][0] == 2
    assert agent.messages[1][1].order.agent_id == 2
    assert agent.messages[1][1].order.side == Side.ASK
    assert agent.messages[1][1].order.fill_price == 200
    assert agent.messages[1][1].order.quantity == 40
    assert agent.messages[2][0] == 1
    assert agent.messages[2][1].order.agent_id == 1
    assert agent.messages[2][1].order.side == Side.BID
    assert agent.messages[2][1].order.fill_price == 100
    assert agent.messages[2][1].order.quantity == 30
    assert agent.messages[3][0] == 2
    assert agent.messages[3][1].order.agent_id == 2
    assert agent.messages[3][1].order.side == Side.ASK
    assert agent.messages[3][1].order.fill_price == 100
    assert agent.messages[3][1].order.quantity == 30


def test_handle_bad_limit_orders():
    book, agent, _ = setup_book_with_orders()

    # Symbol does not match book — discarded silently at ERROR level, no message sent.
    order = MarketOrder(
        agent_id=1,
        time_placed=TIME,
        symbol="BAD",
        quantity=70,
        side=Side.ASK,
    )
    book.handle_market_order(order)
    assert len(agent.messages) == 0

    # Non-integer quantity → OrderRejectedMsg(INVALID_QUANTITY)
    order = MarketOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=1.5,
        side=Side.BID,
    )
    book.handle_market_order(order)
    assert len(agent.messages) == 1
    assert isinstance(agent.messages[0][1], OrderRejectedMsg)
    assert agent.messages[0][1].reason is RejectReason.INVALID_QUANTITY
    agent.reset()

    # Negative quantity → OrderRejectedMsg(INVALID_QUANTITY)
    order = MarketOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=-10,
        side=Side.BID,
    )
    book.handle_market_order(order)
    assert len(agent.messages) == 1
    assert isinstance(agent.messages[0][1], OrderRejectedMsg)
    assert agent.messages[0][1].reason is RejectReason.INVALID_QUANTITY


# ---------------------------------------------------------------------------
# OrderRejectedMsg validation
# ---------------------------------------------------------------------------


def test_order_rejected_zero_quantity():
    """handle_market_order sends OrderRejectedMsg(INVALID_QUANTITY) for quantity=0."""
    agent = FakeExchangeAgent()
    book = OrderBook(agent, SYMBOL)
    order = MarketOrder(
        agent_id=1,
        time_placed=TIME,
        symbol=SYMBOL,
        quantity=0,
        side=Side.BID,
    )
    book.handle_market_order(order)
    assert len(agent.messages) == 1
    assert isinstance(agent.messages[0][1], OrderRejectedMsg)
    assert agent.messages[0][1].reason is RejectReason.INVALID_QUANTITY

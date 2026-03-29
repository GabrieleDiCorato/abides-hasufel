from abides_markets.order_book import OrderBook
from abides_markets.orders import LimitOrder, Side

from . import SYMBOL, TIME, FakeExchangeAgent

# fmt: off


def test_empty_book():
    book = OrderBook(FakeExchangeAgent(), SYMBOL)

    assert book.get_l1_bid_data() is None
    assert book.get_l1_ask_data() is None
    assert book.get_l2_bid_data() == []
    assert book.get_l2_ask_data() == []
    assert book.get_l3_bid_data() == []
    assert book.get_l3_ask_data() == []
    assert book.get_transacted_volume() == (0, 0)

# fmt: on


# ===================================================================
# Large book depth — bisect insertion and L2 data extraction
# ===================================================================


class TestLargeBookDepth:
    """Stress tests with 100+ price levels to verify bisect insertion integrity,
    cancellation from the middle, and L2 depth queries on deep books.
    """

    def test_100_bid_levels_sorted(self):
        """Insert 100 bid price levels in random order → bids strictly descending."""
        agent = FakeExchangeAgent()
        book = OrderBook(agent, SYMBOL)

        # Insert prices in a scrambled order
        prices = list(range(100, 200))
        import random

        rng = random.Random(42)
        rng.shuffle(prices)

        for p in prices:
            order = LimitOrder(1, TIME, SYMBOL, 10, Side.BID, p)
            book.handle_limit_order(order)

        assert len(book.bids) == 100
        for i in range(1, len(book.bids)):
            assert book.bids[i - 1].price > book.bids[i].price

    def test_100_ask_levels_sorted(self):
        """Insert 100 ask price levels in random order → asks strictly ascending."""
        agent = FakeExchangeAgent()
        book = OrderBook(agent, SYMBOL)

        prices = list(range(200, 300))
        import random

        rng = random.Random(42)
        rng.shuffle(prices)

        for p in prices:
            order = LimitOrder(1, TIME, SYMBOL, 10, Side.ASK, p)
            book.handle_limit_order(order)

        assert len(book.asks) == 100
        for i in range(1, len(book.asks)):
            assert book.asks[i - 1].price < book.asks[i].price

    def test_cancel_from_middle_of_deep_book(self):
        """Cancel an order in the middle of a 100-level book → no index corruption."""
        agent = FakeExchangeAgent()
        book = OrderBook(agent, SYMBOL)

        orders = []
        for p in range(100, 200):
            o = LimitOrder(1, TIME, SYMBOL, 10, Side.BID, p)
            book.handle_limit_order(o)
            orders.append(o)

        assert len(book.bids) == 100

        # Cancel the order at price 150 (middle of the book)
        mid_order = orders[50]  # price = 150
        result = book.cancel_order(mid_order)
        assert result is True

        assert len(book.bids) == 99
        remaining_prices = [pl.price for pl in book.bids]
        assert 150 not in remaining_prices
        # Still sorted
        for i in range(1, len(book.bids)):
            assert book.bids[i - 1].price > book.bids[i].price

    def test_l2_depth_query_on_deep_book(self):
        """L2 depth=5 on a 100-level book → returns exactly 5 levels, best first."""
        agent = FakeExchangeAgent()
        book = OrderBook(agent, SYMBOL)

        for p in range(100, 200):
            book.handle_limit_order(LimitOrder(1, TIME, SYMBOL, p, Side.BID, p))

        for p in range(200, 300):
            book.handle_limit_order(LimitOrder(1, TIME, SYMBOL, p, Side.ASK, p))

        bid_l2 = book.get_l2_bid_data(depth=5)
        ask_l2 = book.get_l2_ask_data(depth=5)

        assert len(bid_l2) == 5
        assert len(ask_l2) == 5

        # Best 5 bids: 199, 198, 197, 196, 195
        assert [entry[0] for entry in bid_l2] == [199, 198, 197, 196, 195]
        # Best 5 asks: 200, 201, 202, 203, 204
        assert [entry[0] for entry in ask_l2] == [200, 201, 202, 203, 204]

    def test_l2_depth_exceeds_book_depth(self):
        """L2 depth=50 on a 3-level book → returns only 3 levels."""
        agent = FakeExchangeAgent()
        book = OrderBook(agent, SYMBOL)

        for p in [100, 101, 102]:
            book.handle_limit_order(LimitOrder(1, TIME, SYMBOL, 10, Side.BID, p))

        bid_l2 = book.get_l2_bid_data(depth=50)
        assert len(bid_l2) == 3

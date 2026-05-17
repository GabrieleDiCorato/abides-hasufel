import itertools
from copy import deepcopy

import pytest

from abides_markets.orders import (
    LimitOrder,
    MarketOrder,
    Order,
    Side,
    StopOrder,
    TimeInForce,
)

TIME = 0


def test_order_id_generation():
    # Test incremental ID counter for MarketOrder class
    Order._order_id_generator = itertools.count(0)

    order1 = MarketOrder(1, TIME, "X", 1, True)
    order2 = MarketOrder(1, TIME, "X", 1, True)
    order3 = MarketOrder(1, TIME, "X", 1, True)

    assert order1.order_id == 0
    assert order2.order_id == 1
    assert order3.order_id == 2

    # Test incremental ID counter for LimitOrder class
    Order._order_id_generator = itertools.count(0)

    order1 = LimitOrder(1, TIME, "X", 1, True, 1)
    order2 = LimitOrder(1, TIME, "X", 1, True, 1)
    order3 = LimitOrder(1, TIME, "X", 1, True, 1)

    assert order1.order_id == 0
    assert order2.order_id == 1
    assert order3.order_id == 2

    # Test incremental ID counter for mix of order classes
    Order._order_id_generator = itertools.count(0)

    order1 = MarketOrder(1, TIME, "X", 1, True)
    order2 = LimitOrder(1, TIME, "X", 1, True, 1)

    assert order1.order_id == 0
    assert order2.order_id == 1

    # Test setting duplicate ID does not affect order ID generation
    Order._order_id_generator = itertools.count(0)

    order1 = MarketOrder(1, TIME, "X", 1, True)
    order2 = MarketOrder(1, TIME, "X", 1, True, order_id=0)
    order3 = MarketOrder(1, TIME, "X", 1, True)

    assert order1.order_id == 0
    assert order2.order_id == 0
    assert order3.order_id == 1


def test_order_equality():
    order1 = LimitOrder(1, TIME, "X", 1, True, 1)
    order2 = LimitOrder(1, TIME, "X", 1, True, 1)

    assert order1 == order1
    assert order1 == deepcopy(order1)

    assert order1 != order2


def test_base_order_init():
    with pytest.raises(TypeError):
        Order(1, TIME, "X", 1, True)


# ---------------------------------------------------------------------------
# Phase 2a: __slots__, _slot_values, to_dict (no deepcopy), to_payload_tuple
# ---------------------------------------------------------------------------


def test_slots_reject_arbitrary_attributes():
    """Slotted Order instances must refuse unknown attributes.

    This guards the Phase 2a memory contract: no per-instance ``__dict__``.
    """
    order = MarketOrder(1, TIME, "X", 1, Side.BID)
    with pytest.raises(AttributeError):
        order.foo = 42

    limit = LimitOrder(1, TIME, "X", 1, Side.BID, 100)
    with pytest.raises(AttributeError):
        limit.bar = "nope"

    stop = StopOrder(1, TIME, "X", 1, Side.ASK, 100)
    with pytest.raises(AttributeError):
        stop.baz = object()


def test_slots_no_instance_dict():
    """Slotted instances expose no ``__dict__``."""
    order = MarketOrder(1, TIME, "X", 1, Side.BID)
    assert not hasattr(order, "__dict__")

    limit = LimitOrder(1, TIME, "X", 1, Side.BID, 100)
    assert not hasattr(limit, "__dict__")

    stop = StopOrder(1, TIME, "X", 1, Side.ASK, 100)
    assert not hasattr(stop, "__dict__")


def test_eq_uses_slot_values():
    """Equality compares slot tuples — same type, same data → equal."""
    Order._order_id_generator = itertools.count(0)
    a = LimitOrder(1, TIME, "X", 5, Side.BID, 100)
    Order._order_id_generator = itertools.count(0)
    b = LimitOrder(1, TIME, "X", 5, Side.BID, 100)
    assert a == b

    # Differ in a slot inherited from Order
    Order._order_id_generator = itertools.count(0)
    c = LimitOrder(1, TIME, "X", 6, Side.BID, 100)
    assert a != c

    # Differ in a slot defined on LimitOrder
    Order._order_id_generator = itertools.count(0)
    d = LimitOrder(1, TIME, "X", 5, Side.BID, 101)
    assert a != d


def test_eq_rejects_cross_type():
    """A LimitOrder is never equal to a MarketOrder, even when shared
    slot values match."""
    Order._order_id_generator = itertools.count(0)
    m = MarketOrder(1, TIME, "X", 1, Side.BID)
    Order._order_id_generator = itertools.count(0)
    limit = LimitOrder(1, TIME, "X", 1, Side.BID, 100)
    assert m != limit


def test_to_dict_contains_all_slot_keys_and_no_deepcopy():
    """``to_dict`` returns one entry per slot plus formatted time, and
    does not defensively copy."""
    limit = LimitOrder(
        1,
        TIME,
        "X",
        5,
        Side.BID,
        100,
        is_hidden=True,
        time_in_force=TimeInForce.IOC,
        tag="strat-A",
    )
    d = limit.to_dict()
    expected_keys = {
        "agent_id",
        "time_placed",
        "symbol",
        "quantity",
        "side",
        "order_id",
        "fill_price",
        "tag",
        "limit_price",
        "is_hidden",
        "is_price_to_comply",
        "insert_by_id",
        "is_post_only",
        "time_in_force",
    }
    assert set(d.keys()) == expected_keys
    assert d["limit_price"] == 100
    assert d["is_hidden"] is True
    assert d["time_in_force"] is TimeInForce.IOC
    assert d["tag"] == "strat-A"
    # time_placed is formatted, not raw nanoseconds
    assert isinstance(d["time_placed"], str)
    # No defensive copy: nested mutable values are the same object
    tag_obj: list[int] = [1, 2, 3]
    limit2 = LimitOrder(1, TIME, "X", 5, Side.BID, 100, tag=tag_obj)
    assert limit2.to_dict()["tag"] is tag_obj


def test_to_payload_tuple_limit_order_matches_schema():
    """LimitOrder.to_payload_tuple matches the ORDER_EVENT schema."""
    limit = LimitOrder(
        7,
        TIME,
        "Y",
        9,
        Side.ASK,
        12_345,
        is_hidden=True,
        is_price_to_comply=False,
        time_in_force=TimeInForce.FOK,
        tag="t1",
        order_id=42,
    )
    t = limit.to_payload_tuple()
    assert t == (
        42,  # order_id
        "LIMIT",  # order_kind
        "Y",  # symbol
        Side.ASK,  # side
        9,  # quantity
        12_345,  # limit_price
        None,  # stop_price
        TimeInForce.FOK,  # time_in_force
        True,  # is_hidden
        False,  # is_price_to_comply
        "t1",  # tag
    )
    assert len(t) == 11


def test_to_payload_tuple_market_order_matches_schema():
    """MarketOrder.to_payload_tuple fills price/tif fields with None."""
    m = MarketOrder(3, TIME, "Z", 4, Side.BID, order_id=11, tag="m1")
    t = m.to_payload_tuple()
    assert t == (
        11,
        "MARKET",
        "Z",
        Side.BID,
        4,
        None,
        None,
        None,
        None,
        None,
        "m1",
    )
    assert len(t) == 11


def test_to_payload_tuple_stop_order_matches_schema():
    """StopOrder.to_payload_tuple carries the stop_price; limit_price
    and tif are None."""
    s = StopOrder(2, TIME, "Q", 3, Side.ASK, 9_999, order_id=5, tag="s1")
    t = s.to_payload_tuple()
    assert t == (
        5,
        "STOP",
        "Q",
        Side.ASK,
        3,
        None,
        9_999,
        None,
        None,
        None,
        "s1",
    )
    assert len(t) == 11


def test_order_kind_class_constants():
    """``order_kind`` is the canonical discriminator for typed payloads."""
    assert LimitOrder.order_kind == "LIMIT"
    assert MarketOrder.order_kind == "MARKET"
    assert StopOrder.order_kind == "STOP"


def test_orders_remain_unhashable():
    """Mutable orders must not be hashable, matching pre-Phase-2a contract."""
    m = MarketOrder(1, TIME, "X", 1, Side.BID)
    with pytest.raises(TypeError):
        hash(m)


def test_deepcopy_still_works_under_slots():
    """Custom __deepcopy__ implementations survive the slot conversion."""
    Order._order_id_generator = itertools.count(0)
    limit = LimitOrder(
        1, TIME, "X", 5, Side.BID, 100, time_in_force=TimeInForce.IOC, tag="t"
    )
    limit.fill_price = 99
    copy = deepcopy(limit)
    assert copy is not limit
    assert copy == limit
    assert copy.fill_price == 99
    assert copy.time_in_force is TimeInForce.IOC

    m = MarketOrder(1, TIME, "X", 1, Side.BID, order_id=10)
    m_copy = deepcopy(m)
    assert m_copy is not m
    assert m_copy == m

    s = StopOrder(1, TIME, "X", 1, Side.ASK, 50, order_id=20)
    s_copy = deepcopy(s)
    assert s_copy is not s
    assert s_copy == s


def test_side_is_intenum_with_stable_values():
    """``Side`` is encoded as an IntEnum with stable integer values."""
    assert int(Side.BID) == 1
    assert int(Side.ASK) == 2
    assert Side.BID == 1
    assert Side.ASK == 2
    assert Side.BID != Side.ASK


def test_side_legacy_str_returns_pre_phase_2b_form():
    """``Side.legacy_str()`` returns the old string representation."""
    assert Side.BID.legacy_str() == "BID"
    assert Side.ASK.legacy_str() == "ASK"


def test_time_in_force_is_intenum_with_stable_values():
    """``TimeInForce`` is encoded as an IntEnum with stable integer values."""
    assert int(TimeInForce.GTC) == 1
    assert int(TimeInForce.IOC) == 2
    assert int(TimeInForce.FOK) == 3
    assert int(TimeInForce.DAY) == 4
    assert TimeInForce.GTC == 1


def test_time_in_force_legacy_str_returns_pre_phase_2b_form():
    """``TimeInForce.legacy_str()`` returns the old string representation."""
    assert TimeInForce.GTC.legacy_str() == "GTC"
    assert TimeInForce.IOC.legacy_str() == "IOC"
    assert TimeInForce.FOK.legacy_str() == "FOK"
    assert TimeInForce.DAY.legacy_str() == "DAY"


def test_intenum_equality_preserves_existing_semantics():
    """Enum-to-enum comparisons still work after the IntEnum migration."""
    assert Side.BID == Side.BID
    assert Side.BID is Side.BID
    assert TimeInForce.IOC == TimeInForce.IOC
    assert TimeInForce.IOC is TimeInForce.IOC

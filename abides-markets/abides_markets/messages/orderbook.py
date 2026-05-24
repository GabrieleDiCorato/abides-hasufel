from abc import ABC
from dataclasses import dataclass
from enum import Enum

from abides_core import Message

from ..orders import LimitOrder, Order, StopOrder


class RejectReason(Enum):
    """Reason an order was rejected by the exchange."""

    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"  # reserved; FOK still sends OrderCancelledMsg (FIX-correct)


@dataclass
class OrderBookMsg(Message, ABC):
    pass


@dataclass
class OrderRejectedMsg(OrderBookMsg):
    """Sent to the submitting agent when the exchange rejects an order.

    ``order_id`` matches the rejected order's id; agents can look it up in
    ``self.orders``.  ``reason`` is a ``RejectReason`` enum value.
    """

    order_id: int
    reason: RejectReason


@dataclass
class OrderAcceptedMsg(OrderBookMsg):
    order: LimitOrder


@dataclass
class OrderExecutedMsg(OrderBookMsg):
    order: Order


@dataclass
class OrderCancelledMsg(OrderBookMsg):
    order: LimitOrder


@dataclass
class OrderPartialCancelledMsg(OrderBookMsg):
    new_order: LimitOrder


@dataclass
class OrderModifiedMsg(OrderBookMsg):
    new_order: LimitOrder


@dataclass
class OrderReplacedMsg(OrderBookMsg):
    old_order: LimitOrder
    new_order: LimitOrder


@dataclass
class StopTriggeredMsg(OrderBookMsg):
    """Sent to the agent when their stop order has been triggered.

    The ``order`` field carries the original ``StopOrder``.  The
    resulting market order is submitted automatically by the exchange.
    """

    order: StopOrder

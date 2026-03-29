import logging
import math
from collections import deque

import numpy as np

from abides_core import Message, NanosecondTime
from abides_core.utils import str_to_ns

from ...messages.marketdata import L2SubReqMsg, MarketDataMsg
from ...messages.query import QuerySpreadResponseMsg
from ...models.risk_config import RiskConfig
from ...orders import Side
from ..trading_agent import TradingAgent

logger = logging.getLogger(__name__)

_DEFAULT_WAKE_UP_FREQ: int = str_to_ns("60s")


class MeanReversionAgent(TradingAgent):
    """Contrarian strategy agent using Bollinger-band / z-score mean reversion.

    Maintains a rolling window of mid-price observations and computes the
    z-score: ``(mid - mean) / std``.  Buys when the z-score drops below
    ``-entry_threshold`` (price is unusually low) and sells when it rises above
    ``+entry_threshold`` (price is unusually high).

    This agent does **not** access the oracle — it trades on LOB state only,
    making it the natural complement to :class:`MomentumAgent`.

    Supports both polling (``subscribe=False``) and L2 subscription
    (``subscribe=True``) modes, identical to :class:`MomentumAgent`.
    """

    VALID_STATES = frozenset(
        {"AWAITING_WAKEUP", "AWAITING_SPREAD", "AWAITING_MARKET_DATA"}
    )

    def __init__(
        self,
        id: int,
        symbol,
        starting_cash,
        name: str | None = None,
        type: str | None = None,
        random_state: np.random.RandomState | None = None,
        min_size: int = 20,
        max_size: int = 50,
        wake_up_freq: NanosecondTime = _DEFAULT_WAKE_UP_FREQ,
        poisson_arrival: bool = True,
        order_size_model=None,
        subscribe: bool = False,
        log_orders: bool = False,
        window: int = 20,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.5,
        risk_config: RiskConfig | None = None,
    ) -> None:
        if window < 2:
            raise ValueError(f"window ({window}) must be >= 2.")
        if entry_threshold <= 0:
            raise ValueError(
                f"entry_threshold ({entry_threshold}) must be positive."
            )
        if exit_threshold < 0:
            raise ValueError(
                f"exit_threshold ({exit_threshold}) must be non-negative."
            )
        if exit_threshold >= entry_threshold:
            raise ValueError(
                f"exit_threshold ({exit_threshold}) must be < entry_threshold ({entry_threshold})."
            )

        super().__init__(
            id,
            name,
            type,
            random_state,
            starting_cash,
            log_orders,
            risk_config=risk_config,
        )
        self.symbol = symbol
        self.min_size = min_size
        self.max_size = max_size
        self.size = (
            self.random_state.randint(self.min_size, self.max_size)
            if order_size_model is None
            else None
        )
        self.order_size_model = order_size_model
        self.wake_up_freq = wake_up_freq
        self.poisson_arrival = poisson_arrival
        if self.poisson_arrival:
            self.arrival_rate = self.wake_up_freq

        self.subscribe = subscribe
        self.subscription_requested = False

        self.window: int = window
        self.entry_threshold: float = entry_threshold
        self.exit_threshold: float = exit_threshold
        self.mid_list: deque[int] = deque(maxlen=window)

        self.state = "AWAITING_WAKEUP"

    def kernel_starting(self, start_time: NanosecondTime) -> None:
        super().kernel_starting(start_time)

    def wakeup(self, current_time: NanosecondTime) -> None:
        can_trade = super().wakeup(current_time)
        if self.subscribe and not self.subscription_requested:
            super().request_data_subscription(
                L2SubReqMsg(
                    symbol=self.symbol,
                    freq=int(10e9),
                    depth=1,
                )
            )
            self.subscription_requested = True
            self.state = "AWAITING_MARKET_DATA"
        elif can_trade and not self.subscribe:
            self.get_current_spread(self.symbol)
            self.state = "AWAITING_SPREAD"

    def receive_message(
        self, current_time: NanosecondTime, sender_id: int, message: Message
    ) -> None:
        super().receive_message(current_time, sender_id, message)
        if (
            not self.subscribe
            and self.state == "AWAITING_SPREAD"
            and isinstance(message, QuerySpreadResponseMsg)
        ):
            bid, _, ask, _ = self.get_known_bid_ask(self.symbol)
            self.place_orders(bid, ask)
            self.set_wakeup(current_time + self.get_wake_frequency())
            self.state = "AWAITING_WAKEUP"
        elif (
            self.subscribe
            and self.state == "AWAITING_MARKET_DATA"
            and isinstance(message, MarketDataMsg)
        ):
            bids = self.known_bids.get(self.symbol, [])
            asks = self.known_asks.get(self.symbol, [])
            if bids and asks:
                self.place_orders(bids[0][0], asks[0][0])
            self.state = "AWAITING_MARKET_DATA"

    def place_orders(self, bid: int | None, ask: int | None) -> None:
        """Compute z-score and place a contrarian order if signal fires."""
        if bid is None or ask is None:
            return

        mid = (bid + ask) // 2
        self.mid_list.append(mid)

        if len(self.mid_list) < self.window:
            return

        z = self._z_score()
        if z is None:
            return

        if self.order_size_model is not None:
            self.size = self.order_size_model.sample(random_state=self.random_state)

        if self.size and self.size > 0:
            if z <= -self.entry_threshold:
                # Price unusually low → buy (contrarian)
                self.place_limit_order(
                    self.symbol,
                    quantity=self.size,
                    side=Side.BID,
                    limit_price=ask,
                )
            elif z >= self.entry_threshold:
                # Price unusually high → sell (contrarian)
                self.place_limit_order(
                    self.symbol,
                    quantity=self.size,
                    side=Side.ASK,
                    limit_price=bid,
                )

    def _z_score(self) -> float | None:
        """Compute the z-score of the most recent mid-price observation."""
        n = len(self.mid_list)
        if n < 2:
            return None
        mean = sum(self.mid_list) / n
        var = sum((x - mean) ** 2 for x in self.mid_list) / n
        std = math.sqrt(var)
        if std == 0:
            return None
        return (self.mid_list[-1] - mean) / std

    def get_wake_frequency(self) -> NanosecondTime:
        if not self.poisson_arrival:
            return self.wake_up_freq
        else:
            delta_time = self.random_state.exponential(scale=self.arrival_rate)
            return int(round(delta_time))

"""Context dataclasses passed to extension config ``build()`` methods.

Each extension config (oracle, exchange, latency) receives one of these at
compile time so it can construct the appropriate runtime object without
the compiler needing to know the internals of each config type.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from abides_core import NanosecondTime


@dataclass
class OracleContext:
    """Context passed to ``OracleConfig.build()``."""

    mkt_open: NanosecondTime
    mkt_close: NanosecondTime
    ticker: str
    random_state: np.random.RandomState
    date_ns: NanosecondTime


@dataclass
class ExchangeContext:
    """Context passed to ``ExchangeConfig.build()``."""

    mkt_open: NanosecondTime
    mkt_close: NanosecondTime
    symbols: list[str]
    random_state: np.random.RandomState
    opening_prices: dict[str, int] | None


@dataclass
class LatencyContext:
    """Context passed to ``LatencyConfig.build()``."""

    agent_count: int
    random_state: np.random.RandomState

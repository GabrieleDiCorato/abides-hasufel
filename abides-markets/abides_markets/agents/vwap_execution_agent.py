"""
VWAP (Volume-Weighted Average Price) Execution Agent

Distributes a parent order across time slices according to an expected
volume profile.  When no profile is supplied the agent degrades to
uniform (TWAP-like) slicing.  The default built-in profile is U-shaped
(higher participation near open and close).
"""

from __future__ import annotations

import logging
import math

import numpy as np

from abides_core import NanosecondTime

from ..models.risk_config import RiskConfig
from ..orders import Side
from .base_execution_agent import BaseSlicingExecutionAgent

logger = logging.getLogger(__name__)


def _default_u_profile(n: int) -> list[float]:
    """Generate a simple U-shaped volume profile with *n* buckets.

    Uses a parabola centred at the midpoint so ends are ~2× the middle.
    Returns normalised weights summing to 1.0.
    """
    if n <= 0:
        return []
    if n == 1:
        return [1.0]
    mid = (n - 1) / 2.0
    raw = [1.0 + ((i - mid) / mid) ** 2 for i in range(n)]
    total = sum(raw)
    return [w / total for w in raw]


class VWAPExecutionAgent(BaseSlicingExecutionAgent):
    """Volume-Weighted Average Price execution agent.

    At each wakeup the agent sizes its child order proportionally to a
    volume-profile weight for that slice::

        slice_qty = profile[slice_index] * total_quantity

    Catch-up adjustments redistribute any deficit from earlier partial
    fills across the remaining profile weights.

    Parameters
    ----------
    volume_profile : list[float] | None
        Per-slice weights.  ``None`` → built-in U-shaped curve.
        The profile is normalised internally so any positive weights work.
    """

    def __init__(
        self,
        id: int,
        symbol: str,
        starting_cash: int,
        start_time: NanosecondTime,
        end_time: NanosecondTime,
        freq: NanosecondTime,
        direction: Side = Side.BID,
        quantity: int = 1000,
        trade: bool = True,
        order_style: str = "ioc_limit",
        volume_profile: list[float] | None = None,
        name: str | None = None,
        type: str | None = None,
        random_state: np.random.RandomState | None = None,
        log_orders: bool = False,
        risk_config: RiskConfig | None = None,
    ) -> None:
        super().__init__(
            id=id,
            symbol=symbol,
            starting_cash=starting_cash,
            start_time=start_time,
            end_time=end_time,
            freq=freq,
            direction=direction,
            quantity=quantity,
            trade=trade,
            order_style=order_style,  # type: ignore[arg-type]
            name=name,
            type=type,
            random_state=random_state,
            log_orders=log_orders,
            risk_config=risk_config,
        )

        duration = self.end_time - self.start_time
        self.total_slices: int = max(1, math.ceil(duration / self.freq))
        self.slice_index: int = 0

        # Build / normalise profile
        if volume_profile is None:
            self.volume_profile: list[float] = _default_u_profile(self.total_slices)
        else:
            self.volume_profile = _normalise(volume_profile, self.total_slices)

    # ------------------------------------------------------------------
    # Slice sizing: volume-weighted (with catch-up)
    # ------------------------------------------------------------------
    def _compute_slice_quantity(self, current_time: NanosecondTime) -> int:
        idx = self.slice_index
        self.slice_index += 1

        if idx >= len(self.volume_profile):
            # Past the profile — send remainder
            return self.remaining_quantity

        # Remaining profile weight from this slice onward
        remaining_weight = sum(self.volume_profile[idx:])
        if remaining_weight <= 0:
            return self.remaining_quantity

        # This slice's share of the remaining quantity
        proportion = self.volume_profile[idx] / remaining_weight
        qty = max(1, round(proportion * self.remaining_quantity))
        return min(qty, self.remaining_quantity)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _normalise(profile: list[float], n_slices: int) -> list[float]:
    """Normalise *profile* so it sums to 1.0 and has *n_slices* entries.

    If the profile is shorter than *n_slices* it is padded with the
    mean weight; if longer it is truncated and renormalised.
    """
    if not profile:
        return _default_u_profile(n_slices)

    # Truncate or pad
    if len(profile) > n_slices:
        profile = profile[:n_slices]
    elif len(profile) < n_slices:
        mean_w = sum(profile) / len(profile) if profile else 1.0
        profile = list(profile) + [mean_w] * (n_slices - len(profile))

    total = sum(profile)
    if total <= 0:
        return [1.0 / n_slices] * n_slices
    return [w / total for w in profile]

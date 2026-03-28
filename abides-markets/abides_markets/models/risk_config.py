"""Frozen configuration for position limits and circuit-breaker thresholds.

``RiskConfig`` encapsulates the risk-management knobs accepted by
``TradingAgent``.  It is a frozen dataclass so that thresholds cannot be
mutated during a simulation — mutable runtime state (e.g.
``_circuit_breaker_tripped``, ``_peak_nav``) stays on the agent instance.

Follows the same injection pattern as ``OrderSizeModel``: the config
system builds the object in ``_prepare_constructor_kwargs()`` and the
agent constructor accepts it as a single typed argument.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Immutable risk/position-management thresholds for a trading agent.

    Attributes:
        position_limit: Per-symbol absolute position cap (shares).
            ``None`` disables the check.  Symmetric: allows ``[-N, +N]``.
        position_limit_clamp: When ``True``, orders breaching the limit
            are reduced (clamped) rather than fully rejected.
        max_drawdown: Loss from ``starting_cash`` in **cents** that trips
            the circuit breaker.  ``None`` disables.
        max_order_rate: Maximum orders per tumbling window before the
            circuit breaker trips.  ``None`` disables.
        order_rate_window_ns: Tumbling-window duration in nanoseconds
            for the order-rate check.  Default 1 minute.
    """

    position_limit: int | None = None
    position_limit_clamp: bool = False
    max_drawdown: int | None = None
    max_order_rate: int | None = None
    order_rate_window_ns: int = 60_000_000_000

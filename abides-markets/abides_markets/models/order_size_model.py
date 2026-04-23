"""Order-size mixture model for market simulation.

The model represents the empirically observed bimodal structure of equity
order flow (Gould et al. 2013, Bouchaud et al. 2018):

- **Lognormal component** (relative weight 0.30): captures the heavy-tailed
  distribution of small retail and uninformed orders.  Parameterised in
  log-space as ln(X) ~ N(2.9, 1.2), giving a median of ~18 shares and a
  long right tail.

- **Round-lot normal components** (relative weights 0.55, 0.08, …): model
  institutional clustering at multiples of 100 shares (100, 200, …, 1000).
  Each component has σ = 10% of its centre (e.g. σ=10 for 100-lot, σ=20
  for 200-lot).  Adjacent components are spaced 100 shares apart so 3σ ≤ 30,
  keeping them mostly distinct.

Relative weights are automatically normalised to probabilities — they do not
need to sum to any particular value.

All samples are clipped to ``[1, max_size]`` shares before rounding.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Internal component types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LognormalDist:
    """Log-normal component.  X is log-normally distributed with the given
    log-space mean and sigma: ln(X) ~ N(mean, sigma).

    - ``mean``: log-space mean (μ).  exp(mean) is the median of X.
    - ``sigma``: log-space std-dev (σ).  Larger values give a heavier tail.
    - ``weight``: relative weight (any positive number; need not sum to 1).
    """

    mean: float
    sigma: float
    weight: float

    def sample(self, rs: np.random.RandomState) -> float:
        return float(rs.lognormal(mean=self.mean, sigma=self.sigma))


@dataclass(frozen=True)
class _NormalDist:
    """Normal component.  X ~ N(loc, scale) where both are in shares.

    - ``loc``: centre of the distribution (the round lot, e.g. 100.0).
    - ``scale``: std-dev in shares (σ).  Set to 10% of ``loc`` so that
      adjacent round-lot components remain mostly distinct.
    - ``weight``: relative weight (any positive number; need not sum to 1).
    """

    loc: float
    scale: float
    weight: float

    def sample(self, rs: np.random.RandomState) -> float:
        return float(rs.normal(loc=self.loc, scale=self.scale))


# ---------------------------------------------------------------------------
# Default mixture: 11 components with relative weights
# ---------------------------------------------------------------------------
# Weights are relative — they are automatically normalised at construction
# time and do not need to sum to any particular value.
# fmt: off
_COMPONENTS: tuple[_LognormalDist | _NormalDist, ...] = (
    _LognormalDist(mean=2.9,   sigma=1.2,   weight=0.3000),  # retail tail
    _NormalDist(   loc=100.0,  scale=10.0,  weight=0.5500),  # 100-lot
    _NormalDist(   loc=200.0,  scale=20.0,  weight=0.0800),  # 200-lot
    _NormalDist(   loc=300.0,  scale=30.0,  weight=0.0350),  # 300-lot
    _NormalDist(   loc=400.0,  scale=40.0,  weight=0.0180),  # 400-lot
    _NormalDist(   loc=500.0,  scale=50.0,  weight=0.0080),  # 500-lot
    _NormalDist(   loc=600.0,  scale=60.0,  weight=0.0040),  # 600-lot
    _NormalDist(   loc=700.0,  scale=70.0,  weight=0.0020),  # 700-lot
    _NormalDist(   loc=800.0,  scale=80.0,  weight=0.0015),  # 800-lot
    _NormalDist(   loc=900.0,  scale=90.0,  weight=0.0008),  # 900-lot
    _NormalDist(   loc=1000.0, scale=100.0, weight=0.0007),  # 1000-lot
)
# fmt: on


# ---------------------------------------------------------------------------
# Public model
# ---------------------------------------------------------------------------


class OrderSizeModel:
    """Mixture model for sampling realistic equity order sizes (in shares).

    Combines a lognormal component for small retail-like orders with a set of
    narrow normal components centred on institutional round lots
    (100, 200, …, 1000 shares).  See module docstring for calibration details.

    Parameters
    ----------
    max_size:
        Hard upper bound on sampled order size, in shares.  Any draw above
        this value is clipped to ``max_size``.

        The default (1000) matches the largest round-lot component and prevents
        the lognormal tail from generating unrealistically large noise orders
        (without a cap, ~0.04% of lognormal draws exceed 1000 shares).

        Set lower (e.g. 200) to restrict agents to small orders; set higher
        only if modelling block-trade participants.  Must be ≥ 1.
    """

    def __init__(self, max_size: int = 1000) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self.max_size: int = max_size
        self._components: tuple[_LognormalDist | _NormalDist, ...] = _COMPONENTS
        raw = np.array([c.weight for c in _COMPONENTS], dtype=np.float64)
        self._weights: np.ndarray = raw / raw.sum()

    def sample(self, random_state: np.random.RandomState) -> int:
        """Sample one order size from the mixture.

        Parameters
        ----------
        random_state:
            A ``numpy.random.RandomState`` for reproducible draws.  Pass the
            agent's own ``self.random_state`` to preserve per-agent seed
            isolation across simulation runs.

        Returns
        -------
        int
            An order size in shares, guaranteed to be in ``[1, max_size]``.
        """
        idx = int(random_state.choice(len(self._components), p=self._weights))
        value = self._components[idx].sample(random_state)
        return int(round(min(max(1.0, value), float(self.max_size))))

"""One-off generator for the book_capture reproducibility baseline.

Run from the repository root::

    uv run python abides-markets/tests/data/_capture_book_baseline.py

Produces ``book_capture_baseline_l2.pkl`` next to this file.

The pickle is consumed by ``test_book_capture_reproducibility.py`` to
assert byte-equivalent behaviour of the EventBus-based publish path.
"""

from __future__ import annotations

import pickle
from pathlib import Path

from abides_markets.config_system import SimulationBuilder
from abides_markets.simulation import ResultProfile, run_simulation

# Fixed-seed minimal config: rmsc04 with a 2-minute trading window.
_CONFIG_FN = lambda: (  # noqa: E731
    SimulationBuilder().apply_template("rmsc04").end_time("09:32:00").seed(42).build()
)


def _canonicalize_markets(result) -> dict:
    """Reduce per-symbol market data to a pickle-stable plain dict.

    Pydantic ``model_dump()`` triggers the field serializers on
    L1Snapshots / L2Snapshots, returning plain Python lists in place of
    numpy arrays.
    """
    return {
        symbol: {
            "l1_close": market.l1_close.model_dump(),
            "liquidity": market.liquidity.model_dump(),
            "l1_series": (
                market.l1_series.model_dump() if market.l1_series is not None else None
            ),
            "l2_series": (
                market.l2_series.model_dump() if market.l2_series is not None else None
            ),
            "trades": (
                [t.model_dump() for t in market.trades] if market.trades else []
            ),
        }
        for symbol, market in result.markets.items()
    }


def main() -> None:
    config = _CONFIG_FN()
    result = run_simulation(config, profile=ResultProfile.QUANT)
    payload = _canonicalize_markets(result)

    out_path = Path(__file__).with_name("book_capture_baseline_l2.pkl")
    with out_path.open("wb") as fh:
        pickle.dump(payload, fh, protocol=5)

    n_symbols = len(payload)
    sample = next(iter(payload.values()))
    n_l2 = len(sample["l2_series"]["times_ns"]) if sample["l2_series"] else 0
    n_trades = len(sample["trades"])
    print(
        f"Wrote {out_path} ({out_path.stat().st_size:,} bytes) — "
        f"{n_symbols} symbol(s), {n_l2} L2 snapshots, {n_trades} trades."
    )


if __name__ == "__main__":
    main()

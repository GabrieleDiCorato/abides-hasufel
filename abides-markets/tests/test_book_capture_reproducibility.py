"""Phase 3a — book_capture publish-path reproducibility regression tests.

These tests gate the EventBus migration of OrderBook capture. They load
a fixed-seed baseline pickle produced *before* any OrderBook publish-site
migration (see ``tests/data/_capture_book_baseline.py``) and assert that
the new bus-based path produces byte-equivalent per-symbol market data.

- ``test_l2_byte_equivalent`` — active gate from day 1. Passes today
  against the unmodified publish path and must keep passing through
  Step 6 (publisher migration) and Step 8 (extractor rewire).
- ``test_l1_subset_of_l2`` — ``xfail`` until Step 6 lands, since the
  publisher-side L1 short-circuit only exists after that step.
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import pytest

from abides_markets.config_system import SimulationBuilder
from abides_markets.simulation import ResultProfile, run_simulation

_BASELINE_PATH = Path(__file__).parent / "data" / "book_capture_baseline_l2.pkl"


def _build_config(book_capture: str | None) -> Any:
    builder = (
        SimulationBuilder()
        .from_template("rmsc04")
        .market(end_time="09:32:00")
        .seed(42)
    )
    if book_capture is not None:
        # Pass only book_capture; leaving book_logging at its default avoids
        # the resolver's conflict warning (default True -> "l2") and keeps the
        # legacy publish path (which still gates on book_logging) active for
        # the duration of the Phase 3a migration.
        builder = builder.exchange(book_capture=book_capture)
    return builder.build()


def _canonicalize_markets(result: Any) -> dict:
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


def _load_baseline() -> dict:
    if not _BASELINE_PATH.exists():
        pytest.skip(
            f"Baseline pickle missing at {_BASELINE_PATH}. "
            "Regenerate with tests/data/_capture_book_baseline.py."
        )
    with _BASELINE_PATH.open("rb") as fh:
        return pickle.load(fh)


def _compress_l1(series: dict | None) -> list[tuple[int | None, int | None]]:
    """Drop consecutive-duplicate top-of-book points from an L1 series."""
    if series is None:
        return []
    out: list[tuple[int | None, int | None]] = []
    last: tuple[int | None, int | None] | None = None
    for bp, ap in zip(series["bid_prices"], series["ask_prices"], strict=True):
        point = (bp, ap)
        if point != last:
            out.append(point)
            last = point
    return out


def test_l2_byte_equivalent() -> None:
    """With ``book_capture='l2'``, per-symbol markets must match the baseline."""
    baseline = _load_baseline()
    config = _build_config("l2")
    result = run_simulation(config, profile=ResultProfile.QUANT)
    actual = _canonicalize_markets(result)

    assert set(actual) == set(baseline), "symbol set drifted"
    for symbol in baseline:
        assert actual[symbol] == baseline[symbol], (
            f"market data mismatch for {symbol!r}"
        )


@pytest.mark.xfail(
    reason="Publisher-side L1 short-circuit lands in Step 6", strict=True
)
def test_l1_subset_of_l2() -> None:
    """With ``book_capture='l1'``, l1 data agrees with the l2 baseline and
    l2_series is empty.

    The l1 publish path short-circuits when the top of book is unchanged,
    so the L1-mode series equals the L2-mode series with consecutive
    duplicates dropped. ``l1_close`` is the final top of book and must
    be identical across modes.
    """
    baseline = _load_baseline()
    config = _build_config("l1")
    result = run_simulation(config, profile=ResultProfile.QUANT)
    actual = _canonicalize_markets(result)

    assert set(actual) == set(baseline), "symbol set drifted"
    for symbol, expected in baseline.items():
        got = actual[symbol]
        # l2_series must be empty / absent in l1 mode.
        assert got["l2_series"] is None or not got["l2_series"]["times_ns"], (
            f"l2_series should be empty in l1 mode for {symbol!r}"
        )
        # l1_close (final bid/ask) is mode-invariant.
        assert got["l1_close"] == expected["l1_close"], (
            f"l1_close mismatch for {symbol!r}"
        )
        # l1_series in l1 mode equals the l2-baseline l1_series with
        # consecutive duplicates removed.
        assert _compress_l1(got["l1_series"]) == _compress_l1(expected["l1_series"]), (
            f"l1_series compression mismatch for {symbol!r}"
        )

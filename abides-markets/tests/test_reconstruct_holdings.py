"""Tests for ``abides_markets.utils.reconstruct_holdings``.

Phase 2c reshaped ``HOLDINGS_UPDATED`` from a full snapshot dict into a
per-fill delta tuple ``(symbol, delta_qty, qty_after, cash_after_cents)``
to remove the only variable-size mutable payload from the bus. The
``reconstruct_holdings`` helper exists so any consumer that previously
parsed the snapshot dict can still recover the legacy shape from the
delta stream.
"""

import pandas as pd

from abides_markets.utils import reconstruct_holdings


class TestReconstructHoldings:
    def test_empty_stream_returns_empty_dict(self) -> None:
        assert reconstruct_holdings([]) == {}

    def test_single_buy_fill(self) -> None:
        # Buying 100 of ABM at $10.00 from a starting cash of $100,000.00.
        rows = [("ABM", 100, 100, 9_000_000)]
        assert reconstruct_holdings(rows) == {"ABM": 100, "CASH": 9_000_000}

    def test_buy_then_sell_zeroes_out_symbol(self) -> None:
        rows = [
            ("ABM", 100, 100, 9_000_000),
            ("ABM", -100, 0, 10_000_000),
        ]
        # Symbol removed when qty_after hits zero, CASH reflects last row.
        assert reconstruct_holdings(rows) == {"CASH": 10_000_000}

    def test_multiple_symbols_independent(self) -> None:
        rows = [
            ("ABM", 50, 50, 9_500_000),
            ("XYZ", 200, 200, 7_500_000),
            ("ABM", 25, 75, 7_250_000),
        ]
        assert reconstruct_holdings(rows) == {
            "ABM": 75,
            "XYZ": 200,
            "CASH": 7_250_000,
        }

    def test_first_wake_init_rows_round_trip(self) -> None:
        # Mirrors the first_wake emission pattern: one row per symbol with
        # delta_qty == qty_after.
        rows = [
            ("ABM", 100, 100, 5_000_000),
            ("XYZ", 200, 200, 5_000_000),
        ]
        assert reconstruct_holdings(rows) == {
            "ABM": 100,
            "XYZ": 200,
            "CASH": 5_000_000,
        }

    def test_accepts_dataframe(self) -> None:
        df = pd.DataFrame(
            [
                ("ABM", 100, 100, 9_000_000),
                ("XYZ", 50, 50, 8_500_000),
                ("ABM", -100, 0, 9_500_000),
            ],
            columns=["symbol", "delta_qty", "qty_after", "cash_after_cents"],
        )
        assert reconstruct_holdings(df) == {"XYZ": 50, "CASH": 9_500_000}

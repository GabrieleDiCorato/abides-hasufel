"""Tests for causal order attribution (P1 item 9).

Covers:
- TradeAttribution model construction and immutability
- _extract_trades from order book history
- TRADE_ATTRIBUTION profile flag gating
- Integration with MarketSummary
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# TradeAttribution model
# ---------------------------------------------------------------------------
class TestTradeAttribution:
    def test_construction(self):
        from abides_markets.simulation.result import TradeAttribution

        t = TradeAttribution(
            time_ns=1_000_000,
            passive_agent_id=1,
            aggressive_agent_id=2,
            side="BUY",
            price_cents=10_000,
            quantity=50,
        )
        assert t.time_ns == 1_000_000
        assert t.passive_agent_id == 1
        assert t.aggressive_agent_id == 2
        assert t.side == "BUY"
        assert t.price_cents == 10_000
        assert t.quantity == 50

    def test_frozen(self):
        from abides_markets.simulation.result import TradeAttribution

        t = TradeAttribution(
            time_ns=1,
            passive_agent_id=1,
            aggressive_agent_id=2,
            side="SELL",
            price_cents=100,
            quantity=10,
        )
        with pytest.raises((TypeError, ValidationError)):
            t.quantity = 99


# ---------------------------------------------------------------------------
# _extract_trades
# ---------------------------------------------------------------------------
class TestExtractTrades:
    def _make_history(self):
        return [
            {
                "time": 1_000_000,
                "type": "EXEC",
                "order_id": 1,
                "agent_id": 10,
                "oppos_order_id": 2,
                "oppos_agent_id": 20,
                "side": "BUY",
                "quantity": 50,
                "price": 10_000,
            },
            {
                "time": 2_000_000,
                "type": "LIMIT",
                "order_id": 3,
                "agent_id": 30,
                "side": "SELL",
                "quantity": 100,
                "price": 10_100,
            },
            {
                "time": 3_000_000,
                "type": "EXEC",
                "order_id": 4,
                "agent_id": 30,
                "oppos_order_id": 5,
                "oppos_agent_id": 40,
                "side": "SELL",
                "quantity": 25,
                "price": 10_050,
            },
        ]

    def test_extracts_exec_entries_only(self):
        from abides_markets.simulation.runner import _extract_trades

        class FakeBook:
            history = self._make_history()

        trades = _extract_trades(FakeBook())
        assert len(trades) == 2

    def test_first_trade_fields(self):
        from abides_markets.simulation.runner import _extract_trades

        class FakeBook:
            history = self._make_history()

        trades = _extract_trades(FakeBook())
        t = trades[0]
        assert t.time_ns == 1_000_000
        assert t.passive_agent_id == 10
        assert t.aggressive_agent_id == 20
        assert t.side == "BUY"
        assert t.price_cents == 10_000
        assert t.quantity == 50

    def test_second_trade_fields(self):
        from abides_markets.simulation.runner import _extract_trades

        class FakeBook:
            history = self._make_history()

        trades = _extract_trades(FakeBook())
        t = trades[1]
        assert t.passive_agent_id == 30
        assert t.aggressive_agent_id == 40

    def test_empty_history(self):
        from abides_markets.simulation.runner import _extract_trades

        class FakeBook:
            history: list = []

        trades = _extract_trades(FakeBook())
        assert trades == []

    def test_no_history_attribute(self):
        from abides_markets.simulation.runner import _extract_trades

        class FakeBook:
            pass

        trades = _extract_trades(FakeBook())
        assert trades == []

    def test_exec_without_price_skipped(self):
        from abides_markets.simulation.runner import _extract_trades

        class FakeBook:
            history = [
                {
                    "time": 1,
                    "type": "EXEC",
                    "order_id": 1,
                    "agent_id": 1,
                    "oppos_order_id": 2,
                    "oppos_agent_id": 2,
                    "side": "BUY",
                    "quantity": 10,
                    "price": None,
                },
            ]

        trades = _extract_trades(FakeBook())
        assert trades == []


# ---------------------------------------------------------------------------
# Profile gating
# ---------------------------------------------------------------------------
class TestTradeAttributionProfile:
    def test_trade_attribution_in_quant(self):
        from abides_markets.simulation.profiles import ResultProfile

        assert ResultProfile.TRADE_ATTRIBUTION in ResultProfile.QUANT

    def test_trade_attribution_not_in_summary(self):
        from abides_markets.simulation.profiles import ResultProfile

        assert ResultProfile.TRADE_ATTRIBUTION not in ResultProfile.SUMMARY

    def test_trade_attribution_in_full(self):
        from abides_markets.simulation.profiles import ResultProfile

        assert ResultProfile.TRADE_ATTRIBUTION in ResultProfile.FULL


# ---------------------------------------------------------------------------
# MarketSummary integration
# ---------------------------------------------------------------------------
class TestMarketSummaryTrades:
    def test_market_summary_with_trades(self):
        from abides_markets.simulation.result import (
            L1Close,
            LiquidityMetrics,
            MarketSummary,
            TradeAttribution,
        )

        trades = [
            TradeAttribution(
                time_ns=1,
                passive_agent_id=1,
                aggressive_agent_id=2,
                side="BUY",
                price_cents=100,
                quantity=10,
            )
        ]
        summary = MarketSummary(
            symbol="TEST",
            l1_close=L1Close(time_ns=0),
            liquidity=LiquidityMetrics(
                pct_time_no_bid=0, pct_time_no_ask=0, total_exchanged_volume=10
            ),
            trades=trades,
        )
        assert summary.trades is not None
        assert len(summary.trades) == 1

    def test_market_summary_without_trades(self):
        from abides_markets.simulation.result import (
            L1Close,
            LiquidityMetrics,
            MarketSummary,
        )

        summary = MarketSummary(
            symbol="TEST",
            l1_close=L1Close(time_ns=0),
            liquidity=LiquidityMetrics(
                pct_time_no_bid=0, pct_time_no_ask=0, total_exchanged_volume=0
            ),
        )
        assert summary.trades is None

import tempfile
import time
import unittest
from pathlib import Path

from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import Candle, OrderBookSnapshot
from aitrade.execution.audit_metrics import StrategyFunnel24h, compute_live_strategy_metrics
from aitrade.features.microstructure import MicrostructureMetrics
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.screener.coins_in_play import ScreenerResult
from aitrade.storage import Ledger
from aitrade.strategies.base import MarketContext, TradeSignal


def _dummy_candles(n: int = 80, base: float = 100.0) -> list[Candle]:
    out = []
    ts0 = 1_760_000_000_000
    for i in range(n):
        c = base + i * 0.05
        out.append(Candle(ts0 + i * 900_000, c - 0.1, c + 0.2, c - 0.2, c, 1000.0, 100_000.0))
    return out


def _dummy_ctx(symbol: str = "BTCUSDT"):
    from types import SimpleNamespace as NS
    c15 = _dummy_candles(80, 100.0)
    return NS(
        symbol=symbol,
        candles_15m=c15,
        micro=NS(smoothed_obi=0.1, vamp_divergence_bps=1.0, liquidity_behavior_index=0.9),
        screener=NS(rvol=1.5, spread_pct=0.02),
    )


class TestIntraday18FunnelAndFailClosed(unittest.TestCase):
    def test_wfa_gatekeeper_is_fail_closed_when_artifact_missing(self) -> None:
        orig_path = WalkForwardMetaLabeler.WFA_VALIDATION_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                WalkForwardMetaLabeler.WFA_VALIDATION_PATH = Path(td) / "missing_wfa_validation.json"
                labeler = WalkForwardMetaLabeler(load_settings())
                sig = TradeSignal(
                    symbol="BTCUSDT",
                    side="Buy",
                    strategy_id="T1_DONCHIAN_TSMOM",
                    strategy_name="Donchian TSMOM",
                    timeframe="4h",
                    order_type="Market",
                    entry_price=104.0,
                    stop_loss=100.0,
                    take_profit_1=112.0,
                    take_profit_2=136.0,
                    base_winrate_est=0.50,
                    rationale="Test T1 signal",
                    features={},
                )
                res = labeler.evaluate_signal(sig, _dummy_ctx("BTCUSDT"))
                self.assertFalse(res.passed_ml_filter)
                self.assertEqual(res.features.get("ml_calibration_status"), "WFA_VALIDATION_UNAVAILABLE")
        finally:
            WalkForwardMetaLabeler.WFA_VALIDATION_PATH = orig_path

    def test_strategy_funnel_24h_tracks_idle_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            funnel = StrategyFunnel24h(Path(td) / "funnel.json")
            now = 1_800_000_000.0
            # Старое событие (>24ч) должно очищаться
            funnel.record_scan(
                {"T1_DONCHIAN_TSMOM": {"eligible_window": True, "raw_candidates": 5, "blocked_wfa_ml": 5, "idle_reason": "old"}},
                now_ts=now - 90_000.0,
            )
            funnel.record_scan(
                {
                    "T1_DONCHIAN_TSMOM": {
                        "eligible_window": False,
                        "raw_candidates": 0,
                        "blocked_wfa_ml": 0,
                        "idle_reason": "Вне 30-мин окна закрытия 4h-бара",
                    },
                    "B1_SQUEEZE_BREAKOUT": {
                        "eligible_window": True,
                        "raw_candidates": 1,
                        "blocked_wfa_ml": 0,
                        "idle_reason": "Есть одобренный сигнал B1",
                    },
                },
                now_ts=now,
            )
            funnel.record_guard_block("B1_SQUEEZE_BREAKOUT", "Спред 25 bps > 20 bps", now_ts=now + 1)
            funnel.record_submit("T1_DONCHIAN_TSMOM", now_ts=now + 2)

            rep = funnel.get_summary({"T1_DONCHIAN_TSMOM": 1}, now_ts=now + 5)
            t1 = rep["strategies"]["T1_DONCHIAN_TSMOM"]
            b1 = rep["strategies"]["B1_SQUEEZE_BREAKOUT"]
            self.assertEqual(t1["scans_total"], 1)
            self.assertEqual(t1["scans_eligible"], 0)
            self.assertEqual(t1["submitted"], 1)
            self.assertEqual(t1["filled_24h"], 1)
            self.assertEqual(b1["scans_eligible"], 1)
            self.assertEqual(b1["candidates"], 1)
            self.assertEqual(b1["blocked_guard_exchange"], 1)
            self.assertIn("Спред", b1["last_idle_reason"])

    def test_live_strategy_metrics_isolates_t1_b1_from_legacy_and_traces_slippage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "test_ledger.sqlite3")
            t0_ms = 1_795_000_000_000
            t1_ms = t0_ms + 72 * 3_600_000  # 72 часа (3 дня удержания для T1)

            # 1) T1_DONCHIAN_TSMOM сделка с полным жизненным циклом
            ledger.intent(
                "at-t1-001",
                "BTCUSDT",
                "CORE:T1_DONCHIAN_TSMOM",
                15.0,
                {"side": "Buy", "qty": 0.1, "requestedQty": 0.1, "entry": 100000.0, "sl": 99850.0, "tp": 101200.0, "orderId": "ord-t1-in"},
            )
            ledger.finish("at-t1-001", "FILLED", {"orderId": "ord-t1-in", "orderIds": ["ord-t1-in"]})

            # 2) Legacy сделка (должна попасть в legacy_isolated, не смешиваться с T1)
            ledger.put_records(
                "executions",
                [
                    {
                        "execId": "ex-t1-in",
                        "orderId": "ord-t1-in",
                        "orderLinkId": "at-t1-001",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execType": "Trade",
                        "execQty": "0.1",
                        "closedSize": "0",
                        "execPrice": "100050.0",  # +5 bps slippage
                        "execFee": "5.5",
                        "execTime": str(t0_ms),
                    },
                    {
                        "execId": "ex-t1-out",
                        "orderId": "ord-t1-out",
                        "orderLinkId": "",
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "execType": "Trade",
                        "execQty": "0.1",
                        "closedSize": "0.1",
                        "execPrice": "100650.0",
                        "execFee": "5.5",
                        "execTime": str(t1_ms),
                    },
                ],
                "execId",
            )
            ledger.put_records(
                "closed",
                [
                    {
                        "orderId": "ord-t1-out",
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "qty": "0.1",
                        "closedSize": "0.1",
                        "avgEntryPrice": "100050.0",
                        "avgExitPrice": "100650.0",
                        "closedPnl": "49.0",
                        "updatedTime": str(t1_ms),
                    },
                    {
                        "orderId": "ord-legacy-1",
                        "symbol": "DOGEUSDT",
                        "side": "Buy",
                        "qty": "100",
                        "closedSize": "100",
                        "avgEntryPrice": "0.20",
                        "avgExitPrice": "0.18",
                        "closedPnl": "-20.0",
                        "updatedTime": str(t0_ms - 10_000),
                    },
                ],
                "orderId",
            )

            res = compute_live_strategy_metrics([("autotrade", ledger)], now_ts=(t1_ms / 1000.0) + 60.0)
            self.assertEqual(res["legacy_isolated"]["trades"], 1)
            self.assertAlmostEqual(res["legacy_isolated"]["net_pnl_usd"], -20.0, places=2)

            t1 = res["strategies"]["T1_DONCHIAN_TSMOM"]
            self.assertEqual(t1["trades"], 1)
            self.assertAlmostEqual(t1["net_pnl_usd"], 49.0, places=2)
            self.assertAlmostEqual(t1["avg_hold_hours"], 72.0, places=2)
            self.assertAlmostEqual(t1["avg_slippage_bps"], 5.0, places=2)
            self.assertAlmostEqual(t1["total_fees_usd"], 11.0, places=2)
            # Planned risk = (100000 - 99850) * 0.1 = 15.0 -> 49.0 / 15.0 = 3.267R
            self.assertAlmostEqual(t1["avg_realized_r"], 3.267, places=3)

            self.assertEqual(len(res["execution_traces"]), 1)
            tr = res["execution_traces"][0]
            self.assertEqual(tr["intent_id"], "at-t1-001")
            self.assertEqual(tr["status"], "CLOSED")
            self.assertAlmostEqual(tr["slippage_bps"], 5.0, places=2)


if __name__ == "__main__":
    unittest.main()

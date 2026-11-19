"""
Автотесты математического ядра, индикаторов, микроструктуры стакана L2,
5 интрадей-стратегий, ML-валидатора (DI + Cost-Aware Filter), RiskEngine и конвейера демо-торговли на Bybit V5.
Запуск: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.config import load_settings
from aitrade.engine import AITradeCombineEngine
from aitrade.exchange.bybit_v5 import BybitV5Client
from aitrade.execution.demo_trader import BybitDemoExecutor
from aitrade.features.indicators import (
    calc_adx,
    calc_bollinger_keltner_squeeze,
    calc_natr_and_ratio,
    calc_rvol,
    calc_session_vwap_bands,
    calc_volume_profile,
    determine_mtf_bias,
)
from aitrade.features.microstructure import (
    analyze_microstructure,
    calc_obi,
    calc_vamp_and_microprice,
    calc_weighted_top5_obi,
    savitzky_golay_smooth,
)
from aitrade.risk.risk_engine import RiskEngine


class TestAITradeCombine(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.settings['offline_simulation'] = True
        self.settings['exchange'].update(demo_api_key='',demo_api_secret='',live_api_key='',live_api_secret='')
        offline = patch.object(BybitV5Client, '_public_get', side_effect=RuntimeError('offline test'))
        offline.start(); self.addCleanup(offline.stop)
        self.client = BybitV5Client(self.settings, allow_offline_fallback=True)

    def test_indicators_and_vwap_bands(self) -> None:
        candles = self.client.get_klines("BTCUSDT", interval="15", limit=100)
        self.assertEqual(len(candles), 100)

        vwap = calc_session_vwap_bands(candles)
        self.assertGreater(vwap.vwap, 0.0)
        self.assertGreater(vwap.upper_2sd, vwap.upper_1sd)
        self.assertGreater(vwap.upper_1sd, vwap.vwap)
        self.assertLess(vwap.lower_2sd, vwap.lower_1sd)

        atr14, natr_pct, exp_ratio = calc_natr_and_ratio(candles)
        self.assertGreater(atr14, 0.0)
        self.assertGreater(natr_pct, 0.0)
        self.assertGreater(exp_ratio, 0.2)

        rvol = calc_rvol(candles, lookback=20)
        self.assertGreater(rvol, 0.1)

        adx, pdi, mdi = calc_adx(candles, 14)
        self.assertTrue(0.0 <= adx <= 100.0)

        bb_u, bb_l, kc_u, kc_l, ema20, is_sq = calc_bollinger_keltner_squeeze(candles)
        self.assertGreater(bb_u, bb_l)
        self.assertGreater(kc_u, kc_l)

        vp = calc_volume_profile(candles)
        self.assertGreaterEqual(vp.vah, vp.poc)
        self.assertLessEqual(vp.val, vp.poc)

        bias, e8, e21, e50 = determine_mtf_bias(candles)
        self.assertIn(bias, {"BULLISH_TREND", "BEARISH_TREND", "RANGE", "TRANSITION"})

    def test_l2_microstructure_and_savitzky_golay(self) -> None:
        raw = [0.1, 0.35, -0.2, 0.4, 0.5, 0.3, 0.45, 0.55, 0.5]
        smoothed = savitzky_golay_smooth(raw, window=7, polyorder=2)
        self.assertEqual(len(smoothed), len(raw))

        ob = self.client.get_orderbook("BTCUSDT", limit=25)
        self.assertTrue(len(ob.bids) >= 10 and len(ob.asks) >= 10)
        self.assertLess(ob.best_bid, ob.best_ask)

        obi1 = calc_obi(ob, depth=1)
        obi5 = calc_obi(ob, depth=5)
        w_obi = calc_weighted_top5_obi(ob)
        self.assertTrue(-1.0 <= obi1 <= 1.0)
        self.assertTrue(-1.0 <= obi5 <= 1.0)
        self.assertTrue(-1.0 <= w_obi <= 1.0)

        vamp, w_mid, div_bps = calc_vamp_and_microprice(ob, depth=5)
        self.assertGreater(vamp, 0.0)
        self.assertGreater(w_mid, 0.0)

        c15 = self.client.get_klines("BTCUSDT", interval="15", limit=60)
        trades = self.client.get_recent_trades("BTCUSDT", limit=80)
        micro = analyze_microstructure(c15, ob, self.client.get_orderbook_history("BTCUSDT"), trades)
        self.assertTrue(0.0 <= micro.liquidity_behavior_index <= 1.0)
        self.assertGreater(micro.vpin_tof, 0.0)

    def signal_fixture(self):
        from aitrade.strategies.base import TradeSignal
        return TradeSignal('BTCUSDT','TEST','Fixture','15','Buy','Limit',100.,99.,102.,103.,.65,passed_ml_filter=True)

    def test_risk_engine_5x_liq_and_l2_slippage(self):
        from aitrade.exchange.bybit_v5 import OrderBookSnapshot,OrderBookLevel
        book=OrderBookSnapshot('BTCUSDT',0,[OrderBookLevel(99.99,10000)],[OrderBookLevel(100.01,10000)])
        signal=RiskEngine(self.settings).size_and_validate_signal(self.signal_fixture(),book,1000.)
        self.assertGreater(signal.position_qty,0)
        self.assertEqual(signal.stop_to_liq_multiple,0.)
        self.assertIn("unknown_until_exchange_position",signal.features["liquidation_status"])
        self.assertLessEqual(signal.effective_leverage,4.)
        bad=self.signal_fixture();bad.take_profit_1=100.1
        bad=RiskEngine(self.settings).size_and_validate_signal(bad,book,1000.)
        self.assertEqual(bad.debate_verdict,'REJECTED')

    def test_demo_executor_and_walk_forward_backtest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "test_demo.sqlite3"
            engine = AITradeCombineEngine(self.settings)
            engine.demo_executor = BybitDemoExecutor(self.client, self.settings, db_path=tmp_db)
            signal=self.signal_fixture()
            signal.position_qty=1.;signal.position_size_usd=100.;signal.effective_leverage=1.
            result=engine.demo_executor.execute_signal(signal)
            self.assertEqual(result['execution_venue'],'LOCAL_BYBIT_DEMO_SIM')
            journal = engine.demo_executor.list_recent_trades()
            self.assertEqual(len(journal), 1)

        # Production research never substitutes synthetic candles for a network error.
        with self.assertRaises(RuntimeError):
            WalkForwardBacktester(self.settings).run_comparison_backtest(symbols=['BTCUSDT'])


if __name__ == "__main__":
    unittest.main()

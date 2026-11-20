# -*- coding: utf-8 -*-
import unittest
from datetime import datetime, timezone

from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import Candle, OrderBookLevel, OrderBookSnapshot
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.risk.risk_engine import RiskEngine
from aitrade.strategies.base import TradeSignal
from aitrade.strategies.booster_breakout import is_weekend_chop_utc


class TestIntraday13LiveWiring(unittest.TestCase):
    def test_weekend_chop_filter_boundaries(self):
        # Суббота 17:59 UTC -> разрешено
        sat_ok = int(datetime(2026, 11, 21, 17, 59, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertFalse(is_weekend_chop_utc(sat_ok))
        # Суббота 18:00 UTC -> блок
        sat_block = int(datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertTrue(is_weekend_chop_utc(sat_block))
        # Воскресенье 14:00 UTC (US Sunday morning) -> блок
        sun_block = int(datetime(2026, 11, 22, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertTrue(is_weekend_chop_utc(sun_block))
        # Воскресенье 22:00 UTC (Monday Asia Open) -> разрешено
        sun_ok = int(datetime(2026, 11, 22, 22, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertFalse(is_weekend_chop_utc(sun_ok))

    def test_risk_engine_t1_volatility_targeting(self):
        settings = load_settings()
        re = RiskEngine(settings)
        ob = OrderBookSnapshot(
            symbol="BTCUSDT",
            timestamp=1_760_000_000_000,
            bids=[OrderBookLevel(100.0 - i * 0.01, 5000.0) for i in range(10)],
            asks=[OrderBookLevel(100.01 + i * 0.01, 5000.0) for i in range(10)],
        )
        sig_low_vol = TradeSignal(
            symbol="BTCUSDT",
            strategy_id="T1_DONCHIAN_TSMOM",
            strategy_name="T1",
            timeframe="240",
            side="Buy",
            order_type="Market",
            entry_price=100.0,
            stop_loss=97.0,
            take_profit_1=106.0,
            take_profit_2=112.0,
            base_winrate_est=0.45,
            features={"vol_scale": 1.2},
        )
        sig_high_vol = TradeSignal(
            symbol="BTCUSDT",
            strategy_id="T1_DONCHIAN_TSMOM",
            strategy_name="T1",
            timeframe="240",
            side="Buy",
            order_type="Market",
            entry_price=100.0,
            stop_loss=97.0,
            take_profit_1=106.0,
            take_profit_2=112.0,
            base_winrate_est=0.45,
            features={"vol_scale": 0.5},
        )
        r_low = re.size_and_validate_signal(sig_low_vol, ob, 1000.0)
        r_high = re.size_and_validate_signal(sig_high_vol, ob, 1000.0)
        self.assertGreater(r_low.position_size_usd, r_high.position_size_usd * 2.0)

    def test_meta_labeler_t1_no_synthetic_15m_samples_and_approved(self):
        ml = WalkForwardMetaLabeler(load_settings())
        self.assertIn("T1_DONCHIAN_TSMOM", ml.WFA_APPROVED_STRATEGIES)
        bars = [Candle(1_760_000_000_000 + i * 900_000, 100.0, 101.0, 99.0, 100.5, 100.0, 10000.0) for i in range(120)]
        X, y, r = ml._extract_historical_matrix(bars, 1.0, strategy_id="T1_DONCHIAN_TSMOM")
        self.assertEqual(X, [])
        self.assertEqual(y, [])

    def test_settings_universe_core_6_booster_and_12_autotrade(self):
        s = load_settings()
        self.assertEqual(len(s["universe"]["booster_pairs"]), 6)
        self.assertEqual(len(s["universe"]["autotrade_pairs"]), 12)
        self.assertEqual(len(s["universe"]["core_pairs"]), 12)


if __name__ == "__main__":
    unittest.main()

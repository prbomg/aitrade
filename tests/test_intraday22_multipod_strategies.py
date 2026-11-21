import datetime
import unittest

from aitrade.exchange.bybit_v5 import Candle
from aitrade.strategies.mtf_combo_breakout import generate_mtf_combo_signal
from aitrade.strategies.oi_trend_expansion import generate_oi_trend_expansion_signal
from aitrade.strategies.orb_session_breakout import generate_orb_session_signal
from aitrade.strategies.rs_btc_breakout import generate_rs_btc_signal


class TestIntraday22MultiPodStrategies(unittest.TestCase):
    def test_b4_orb_session_breakout_fires_on_fresh_range_break(self) -> None:
        # 2026-11-20 00:00 UTC старт Азии
        day_start = int(datetime.datetime(2026, 11, 20, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        bars = []
        # 20 баров накануне (19:00..23:45)
        for i in range(-20, 0):
            bars.append(Candle(day_start + i * 900_000, 100.0, 100.5, 99.5, 100.0, 1000.0, 100_000.0))
        # Первые 4 бара Азии (00:00..00:45): диапазон 99.5..100.8
        for i in range(4):
            bars.append(Candle(day_start + i * 900_000, 100.0, 100.8, 99.5, 100.2, 1000.0, 100_000.0))
        # 01:00 UTC — свеча внутри диапазона
        bars.append(Candle(day_start + 4 * 900_000, 100.2, 100.7, 100.0, 100.6, 1000.0, 100_000.0))
        # 01:15 UTC — пробой вверх > 100.8
        bars.append(Candle(day_start + 5 * 900_000, 100.6, 101.6, 100.5, 101.5, 2500.0, 250_000.0))

        sig = generate_orb_session_signal("SOLUSDT", bars, rvol=1.6)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B4_ORB_SESSION_BREAKOUT")
        self.assertEqual(sig.side, "Buy")
        self.assertEqual(sig.take_profit_1, sig.take_profit_2)

    def test_t2_rs_btc_breakout_fires_for_outperforming_alt(self) -> None:
        ts0 = 1_760_000_000_000
        # BTC плоский (+0.5% за 24ч)
        btc_15m = [Candle(ts0 + i * 900_000, 60000.0, 60100.0, 59900.0, 60000.0 + i * 3.0, 100.0, 6_000_000.0) for i in range(100)]
        # Альт вырос на +6% за 24ч и пробивает 12h хай
        alt_1h = [Candle(ts0 + i * 3_600_000, 100.0 + i * 0.15, 101.0 + i * 0.15, 99.5 + i * 0.15, 100.2 + i * 0.15, 1000.0, 100_000.0) for i in range(30)]
        alt_1h.append(Candle(ts0 + 30 * 3_600_000, 104.8, 107.5, 104.5, 107.2, 3000.0, 320_000.0))

        sig = generate_rs_btc_signal("SUIUSDT", alt_1h, btc_15m)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "T2_RS_BTC_BREAKOUT")
        self.assertEqual(sig.side, "Buy")

    def test_b5_oi_trend_expansion_fires_on_oi_growth_and_breakout(self) -> None:
        ts0 = 1_760_000_000_000
        bars = [Candle(ts0 + i * 900_000, 100.0, 100.5, 99.5, 100.1, 1000.0, 100_000.0) for i in range(25)]
        bars.append(Candle(ts0 + 25 * 900_000, 100.2, 101.5, 100.1, 101.3, 2500.0, 250_000.0))
        oi_hist = [
            {"timestamp": str(ts0 + 22 * 900_000), "openInterest": "100000"},
            {"timestamp": str(ts0 + 23 * 900_000), "openInterest": "100600"},
            {"timestamp": str(ts0 + 24 * 900_000), "openInterest": "101200"},
            {"timestamp": str(ts0 + 25 * 900_000), "openInterest": "102500"},
        ]
        sig = generate_oi_trend_expansion_signal("AVAXUSDT", bars, oi_hist)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B5_OI_TREND_EXPANSION")
        self.assertEqual(sig.side, "Buy")

    def test_c2_mtf_combo_breakout_combines_4_factors(self) -> None:
        day_start = int(datetime.datetime(2026, 11, 20, 0, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
        bars_4h = [Candle(day_start + i * 14_400_000, 90.0 + i * 0.4, 91.0 + i * 0.4, 89.5 + i * 0.4, 90.5 + i * 0.4, 1000.0, 100_000.0) for i in range(60)]
        btc_15m = [Candle(day_start + i * 900_000, 60000.0, 60050.0, 59950.0, 60000.0, 100.0, 6_000_000.0) for i in range(100)]
        bars_15m = [Candle(day_start + i * 900_000, 100.0, 100.5, 99.5, 100.2, 1000.0, 100_000.0) for i in range(35)]
        bars_1h = [Candle(day_start + i * 3_600_000, 100.0 + i * 0.08, 101.0 + i * 0.08, 99.5 + i * 0.08, 100.2 + i * 0.08, 1000.0, 100_000.0) for i in range(30)]
        bars_1h.append(Candle(day_start + 30 * 3_600_000, 102.5, 104.8, 102.4, 104.5, 3000.0, 310_000.0))

        sig = generate_mtf_combo_signal("LINKUSDT", bars_15m, bars_1h, bars_4h, btc_15m)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "C2_MTF_COMBO_BREAKOUT")
        self.assertEqual(sig.side, "Buy")


if __name__ == "__main__":
    unittest.main()

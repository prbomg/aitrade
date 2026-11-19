import json
import unittest
from pathlib import Path

from aitrade.config import BASE_DIR
from aitrade.exchange.bybit_v5 import Candle, OrderBookLevel, OrderBookSnapshot
from aitrade.screener.coins_in_play import CoinsInPlayScreener


class Stage5ConfigScreenerUiTests(unittest.TestCase):
    def test_settings_has_split_universe_and_liquid_hours(self):
        cfg = json.loads((BASE_DIR / "config" / "settings.json").read_text(encoding="utf-8"))
        uni = cfg["universe"]
        self.assertIn("autotrade_pairs", uni)
        self.assertIn("booster_pairs", uni)
        self.assertEqual(len(uni["autotrade_pairs"]), 12)
        for thin in ("POPCATUSDT", "WIFUSDT", "1000BONKUSDT"):
            self.assertNotIn(thin, uni["autotrade_pairs"])
            self.assertNotIn(thin, uni["booster_pairs"])
        liq_win = cfg["sessions_utc"]["autotrade_liquid_window"]
        self.assertEqual(liq_win["start_hour"], 7)
        self.assertEqual(liq_win["end_hour"], 20)

    def test_screener_result_includes_last_price(self):
        scr = CoinsInPlayScreener({"screener": {"min_24h_turnover_usd": 1000}})
        start_ms = 1_700_000_000_000
        bars_15 = [Candle(start_ms + i * 900_000, 100, 101, 99, 102.5, 500, 50000) for i in range(30)]
        bars_1h = [Candle(start_ms + i * 3_600_000, 100, 101, 99, 102.5, 2000, 200000) for i in range(30)]
        ob = OrderBookSnapshot("BTCUSDT", start_ms, [OrderBookLevel(102.4, 10)], [OrderBookLevel(102.6, 10)])
        res = scr.evaluate_coin("BTCUSDT", {"turnover24h": "50000000", "fundingRate": "0.0001"}, bars_15, bars_1h, ob)
        self.assertAlmostEqual(res.last_price, 102.5, places=4)

    def test_dashboard_html_has_fixed_buttons_and_training_hydration(self):
        html = (BASE_DIR / "dashboard_report.html").read_text(encoding="utf-8")
        for btn_id in ("btn-refresh-xnews", "btn-toggle-xnews", "btn-run-ml-cal", "btn-toggle-ml-cal"):
            self.assertIn(f'getElementById("{btn_id}")', html)
        self.assertIn("window._boosterTrainingMode = Boolean(b.training_unlimited_mode);", html)
        self.assertIn("vault_usd", html)


if __name__ == "__main__":
    unittest.main()

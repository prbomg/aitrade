# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import BybitV5Client, Candle


class TestIntraday11MomentumWFA(unittest.TestCase):
    def test_get_funding_history_paginated_and_cache(self):
        settings = load_settings()
        client = BybitV5Client(settings, allow_offline_fallback=False)
        eight_h = 8 * 3_600_000
        end_ms = 1_760_000_000_000
        # 250 точек (2 страницы по 200 и 50)
        all_rows = [
            {
                "fundingRateTimestamp": str(end_ms - i * eight_h),
                "fundingRate": f"{0.0001 + (i % 5) * 0.00005:.6f}",
            }
            for i in range(250)
        ]
        calls = []

        def fake_public_get(path, params, timeout=4.5):
            self.assertEqual(path, "/v5/market/funding/history")
            calls.append(params["endTime"])
            cursor = int(params["endTime"])
            eligible = [r for r in all_rows if int(r["fundingRateTimestamp"]) <= cursor]
            return {"list": eligible[: int(params.get("limit", 200))]}

        client._public_get = fake_public_get

        with tempfile.TemporaryDirectory() as tmp:
            res = client.get_funding_history_paginated(
                "BTCUSDT", days=80, end_ms=end_ms, cache_dir=tmp, max_pages=5
            )
            self.assertGreaterEqual(len(res), 240)
            self.assertEqual(len(calls), 2)
            # Строго возрастающий порядок timestamp
            self.assertTrue(all(res[i][0] < res[i + 1][0] for i in range(len(res) - 1)))

            cache_file = Path(tmp) / "BTCUSDT_funding_80d.json"
            self.assertTrue(cache_file.exists())

            # Повторный вызов берёт из кэша без сетевых запросов
            calls.clear()
            res2 = client.get_funding_history_paginated(
                "BTCUSDT", days=80, end_ms=end_ms, cache_dir=tmp, max_pages=5
            )
            self.assertEqual(len(calls), 0)
            self.assertEqual(len(res2), len(res))

    def test_run_momentum_funding_wfa_timestamp_alignment_and_pit_funding(self):
        bt = WalkForwardBacktester(load_settings())
        hour_ms = 3_600_000
        start_ts = 1_750_000_000_000
        # 240 часовых баров (10 дней), BTCUSDT растёт (Long), ETHUSDT падает (Short)
        # У ETHUSDT сдвигаем начало на 5 часов позже, чтобы проверить строгое выравнивание по timestamp
        btc_bars = []
        eth_bars = []
        px_b = 100.0
        px_e = 100.0
        for i in range(240):
            ts = start_ts + i * hour_ms
            o_b = px_b
            px_b = px_b * 1.0035
            btc_bars.append(Candle(ts, o_b, px_b * 1.004, o_b * 0.996, px_b, 1000.0, 100000.0))

            if i >= 5:
                o_e = px_e
                px_e = px_e * 0.9965
                eth_bars.append(Candle(ts, o_e, o_e * 1.004, px_e * 0.996, px_e, 1000.0, 100000.0))

        # Исторический фандинг: дешёвый для BTC (<= 0.00015), перегретый для ETH (>= 0.00005)
        fr_hist = {
            "BTCUSDT": [(start_ts + h * hour_ms, -0.0001) for h in range(0, 240, 8)],
            "ETHUSDT": [(start_ts + h * hour_ms, 0.0003) for h in range(0, 240, 8)],
        }

        res = bt.run_momentum_funding_wfa(
            {"BTCUSDT": btc_bars, "ETHUSDT": eth_bars},
            funding_history_by_symbol=fr_hist,
            n_cycles=6,
            mc_iterations=200,
            liquid_window_only=False,
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["n_cycles"], 6)
        self.assertEqual(len(res["cycles"]), 6)
        self.assertIn("wfe", res)
        self.assertIn("wfe_passed", res)
        self.assertIn("oos_summary", res)
        self.assertIn("oos_stress_2x", res)
        self.assertIn("monte_carlo", res)
        self.assertGreater(res["oos_summary"]["total_trades"], 0)


if __name__ == "__main__":
    unittest.main()

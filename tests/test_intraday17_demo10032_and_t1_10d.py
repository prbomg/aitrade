# -*- coding: utf-8 -*-
import unittest

from aitrade.exchange.bybit_v5 import Candle
from aitrade.exchange.transport import ExchangeError
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.strategies.donchian_tsmom import generate_donchian_tsmom_signal


class TestIntraday17Demo10032AndT110d(unittest.TestCase):
    def test_ensure_isolated_margin_allows_10032_only_on_demo_not_mainnet(self):
        spm_demo = SmartPositionManager("k", "s", base_url="https://api-demo.bybit.com")

        def fail_10032(endpoint, body):
            raise ExchangeError("Bybit 10032: Demo trading is not supported", code=10032)

        spm_demo.signed_post = fail_10032
        res_demo = spm_demo.ensure_isolated_margin("BTCUSDT", leverage=3)
        self.assertTrue(res_demo["ok"])
        self.assertEqual(res_demo["mode"], "DEMO_DEFAULT")

        # На Mainnet ошибка 10032 (или любая ошибка переключения) строго блокирует вход!
        spm_main = SmartPositionManager("k", "s", base_url="https://api.bybit.com")
        spm_main.signed_post = fail_10032
        res_main = spm_main.ensure_isolated_margin("BTCUSDT", leverage=3)
        self.assertFalse(res_main["ok"])

    def test_t1_donchian_tsmom_defaults_8r_and_60_bars(self):
        four_h = 14_400_000
        start_ts = 1_750_000_000_000
        bars = []
        px = 100.0
        for i in range(130):
            ts = start_ts + i * four_h
            o = px
            px = (100.0 + (0.4 if i % 2 == 0 else -0.4)) if i < 95 else (px * 1.01)
            bars.append(Candle(ts, o, max(o, px) * 1.003, min(o, px) * 0.997, px, 1000.0, 100000.0))

        sig = None
        for idx in range(96, len(bars) - 1):
            sig = generate_donchian_tsmom_signal("BTCUSDT", bars[:idx], reference_open_px=bars[idx].open)
            if sig is not None:
                break
        self.assertIsNotNone(sig)
        self.assertEqual(sig.features["max_hold_bars"], 60.0)
        sl_dist = abs(sig.entry_price - sig.stop_loss)
        tp_dist = abs(sig.take_profit_2 - sig.entry_price)
        self.assertAlmostEqual(tp_dist / sl_dist, 8.0, places=3)


if __name__ == "__main__":
    unittest.main()

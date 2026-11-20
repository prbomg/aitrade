# -*- coding: utf-8 -*-
import unittest

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import Candle
from aitrade.strategies.donchian_tsmom import (
    aggregate_4h,
    generate_donchian_tsmom_signal,
)


class TestIntraday12Research(unittest.TestCase):
    def _make_4h_trend_bars(self, n: int = 240, direction: int = 1, start_ts: int = 1_750_000_000_000):
        four_h = 14_400_000
        bars = []
        px = 100.0
        for i in range(n):
            ts = start_ts + i * four_h
            if i < 100:
                # Боковик перед пробоем
                o = px
                px = 100.0 + (0.6 if i % 2 == 0 else -0.6)
                bars.append(Candle(ts, o, max(o, px) + 0.4, min(o, px) - 0.4, px, 1000.0, 100000.0))
            else:
                o = px
                step = 1.008 if direction > 0 else 0.992
                px = px * step
                bars.append(Candle(ts, o, max(o, px) * 1.004, min(o, px) * 0.996, px, 1500.0, 150000.0))
        return bars

    def test_aggregate_4h_and_donchian_tsmom_signal(self):
        bars_4h = self._make_4h_trend_bars(n=140, direction=1)
        # Повторная агрегация уже 4h баров возвращает исходный ряд
        self.assertEqual(len(aggregate_4h(bars_4h)), len(bars_4h))

        # Находим первый свежий пробой вверх после фазы боковика
        sigs = []
        for idx in range(95, len(bars_4h) - 1):
            s = generate_donchian_tsmom_signal(
                "BTCUSDT", bars_4h[:idx], reference_open_px=bars_4h[idx].open, funding_rate=0.0001
            )
            if s is not None:
                sigs.append(s)
        self.assertGreater(len(sigs), 0)
        first = sigs[0]
        self.assertEqual(first.strategy_id, "T1_DONCHIAN_TSMOM")
        self.assertEqual(first.side, "Buy")
        self.assertGreater(first.take_profit_2, first.entry_price)
        self.assertLess(first.stop_loss, first.entry_price)
        self.assertGreaterEqual(first.features["vol_scale"], 0.35)
        self.assertLessEqual(first.features["vol_scale"], 1.80)

    def test_run_donchian_tsmom_wfa_structure(self):
        bt = WalkForwardBacktester(load_settings())
        bars_up = self._make_4h_trend_bars(n=260, direction=1)
        bars_dn = self._make_4h_trend_bars(n=260, direction=-1)
        res = bt.run_donchian_tsmom_wfa(
            {"BTCUSDT": bars_up, "ETHUSDT": bars_dn},
            n_cycles=6,
            mc_iterations=200,
            use_vol_targeting=True,
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["n_cycles"], 6)
        self.assertEqual(len(res["cycles"]), 6)
        self.assertIn("wfe", res)
        self.assertIn("oos_summary", res)
        self.assertIn("oos_stress_2x", res)
        self.assertIn("monte_carlo", res)

    def test_run_funding_carry_backtest_collects_positive_funding_and_closes_on_flip(self):
        bt = WalkForwardBacktester(load_settings())
        eight_h = 8 * 3_600_000
        start_ts = 1_750_000_000_000
        # 30 интервалов устойчивого высокого фандинга (+0.04%/8ч), затем 10 интервалов отрицательного (-0.01%/8ч)
        hist_btc = [(start_ts + i * eight_h, 0.0004 if i < 30 else -0.0001) for i in range(40)]
        hist_eth = [(start_ts + i * eight_h, 0.00005) for i in range(40)]

        res = bt.run_funding_carry_backtest(
            {"BTCUSDT": hist_btc, "ETHUSDT": hist_eth},
            leverage=1.5,
            entry_threshold_8h=0.00025,
            exit_threshold_8h=0.00005,
            min_intervals=9,
        )
        self.assertEqual(res["total_trades"], 1)
        self.assertGreater(res["net_return_1x_pct"], 0.0)
        self.assertLess(res["net_return_2x_pct"], res["net_return_1x_pct"])
        self.assertEqual(res["win_rate_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()

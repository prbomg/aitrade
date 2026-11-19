import math
import tempfile
import unittest
from pathlib import Path

from aitrade.backtest.walk_forward import (
    WalkForwardBacktester,
    compute_daily_sharpe_and_dsr,
    run_monte_carlo_permutation,
)
from aitrade.exchange.bybit_v5 import BybitV5Client, Candle
from aitrade.features.indicators import (
    calc_atr_percentile,
    calc_hurst_exponent,
    calc_supertrend,
)
from aitrade.ml.regime_hmm import RegimeHMM
from aitrade.strategies.base import TradeSignal
from aitrade.strategies.regime_router import classify_regime, has_recent_gaps


def _make_trend_1h(n: int = 120, direction: int = 1, start_ms: int = 1_700_000_000_000) -> list[Candle]:
    bars = []
    px = 100.0
    for i in range(n):
        step = 0.0055 * direction if i >= 40 else 0.001 * direction
        nxt = px * (1.0 + step)
        rng = abs(nxt - px) + (0.9 if i >= 70 else 0.25)
        bars.append(
            Candle(
                timestamp=start_ms + i * 3_600_000,
                open=px,
                high=max(px, nxt) + rng * 0.4,
                low=min(px, nxt) - rng * 0.4,
                close=nxt,
                volume=1000.0,
                turnover=1000.0 * nxt,
            )
        )
        px = nxt
    return bars


def _make_range_1h(n: int = 120, start_ms: int = 1_700_000_000_000) -> list[Candle]:
    bars = []
    for i in range(n):
        # Сначала высокая волатильность, затем сжатие во флэт с жёстким mean-reversion
        amp = 2.5 if i < 60 else 0.25
        c_px = 100.0 + (amp if i % 2 == 0 else -amp)
        o_px = 100.0
        bars.append(
            Candle(
                timestamp=start_ms + i * 3_600_000,
                open=o_px,
                high=max(o_px, c_px) + amp * 0.2,
                low=min(o_px, c_px) - amp * 0.2,
                close=c_px,
                volume=1000.0,
                turnover=1000.0 * c_px,
            )
        )
    return bars


class Stage1WfaAndRegimeTests(unittest.TestCase):
    def test_atr_percentile_range_and_extremes(self):
        bars = _make_trend_1h(120, direction=1)
        pct = calc_atr_percentile(bars, atr_period=14, lookback=100)
        self.assertGreaterEqual(pct, 60.0)
        self.assertLessEqual(pct, 100.0)

        range_bars = _make_range_1h(120)
        pct_low = calc_atr_percentile(range_bars, atr_period=14, lookback=100)
        self.assertLess(pct_low, 40.0)

    def test_hurst_exponent_distinguishes_trend_vs_range(self):
        trend_closes = [c.close for c in _make_trend_1h(120, direction=1)]
        range_closes = [c.close for c in _make_range_1h(120)]
        h_trend = calc_hurst_exponent(trend_closes, window=100)
        h_range = calc_hurst_exponent(range_closes, window=100)
        self.assertGreater(h_trend, 0.55)
        self.assertLess(h_range, 0.45)

    def test_supertrend_direction_matches_trend(self):
        up_bars = _make_trend_1h(80, direction=1)
        down_bars = _make_trend_1h(80, direction=-1)
        _, up_dirs = calc_supertrend(up_bars, period=10, multiplier=3.0)
        _, down_dirs = calc_supertrend(down_bars, period=10, multiplier=3.0)
        self.assertEqual(up_dirs[-1], 1)
        self.assertEqual(down_dirs[-1], -1)

    def test_regime_router_classifies_trend_range_and_recent_gaps_only(self):
        up = _make_trend_1h(120, direction=1)
        down = _make_trend_1h(120, direction=-1)
        rng = _make_range_1h(120)

        d_up = classify_regime(up)
        d_down = classify_regime(down)
        d_rng = classify_regime(rng)

        self.assertEqual(d_up.regime, "TREND_UP")
        self.assertIn("BOOSTER", d_up.allowed_modules)
        self.assertEqual(d_down.regime, "TREND_DOWN")
        self.assertEqual(d_rng.regime, "RANGE")
        self.assertIn("AUTOTRADE_MR", d_rng.allowed_modules)

        # Гэп 80 баров назад (вне последних 60) НЕ должен ломать классификатор
        old_gap = list(up)
        old_gap[10] = Candle(old_gap[10].timestamp - 3_600_000, 100, 101, 99, 100, 1000, 100000)
        self.assertFalse(has_recent_gaps(old_gap, 3_600_000, check_bars=60))
        self.assertEqual(classify_regime(old_gap).regime, "TREND_UP")

        # Гэп внутри последних 60 баров -> NO_REGIME
        recent_gap = list(up)
        recent_gap[-5] = Candle(recent_gap[-5].timestamp + 7_200_000, 100, 101, 99, 100, 1000, 100000)
        self.assertTrue(has_recent_gaps(recent_gap, 3_600_000, check_bars=60))
        self.assertEqual(classify_regime(recent_gap).regime, "NO_REGIME")

    def test_hmm_safe_fallback_without_crash(self):
        hmm = RegimeHMM()
        res = hmm.predict(_make_trend_1h(120, direction=1))
        self.assertIn(res.state, ("BULL_TREND", "SIDEWAYS", "BEAR_TREND", "NO_REGIME"))
        self.assertGreaterEqual(res.confidence, 0.0)
        self.assertLessEqual(res.confidence, 1.0)

    def test_daily_sharpe_dsr_and_monte_carlo(self):
        # Недостаточно дней -> None
        self.assertEqual(compute_daily_sharpe_and_dsr([0.01, -0.005]), (None, None))
        daily = [0.008, -0.003, 0.012, 0.004, -0.002, 0.009, 0.006, -0.004, 0.011, 0.005]
        sr, dsr = compute_daily_sharpe_and_dsr(daily, n_trials=5)
        self.assertIsNotNone(sr)
        self.assertGreater(sr, 0.0)
        self.assertIsNotNone(dsr)
        self.assertGreaterEqual(dsr, 0.0)
        self.assertLessEqual(dsr, 1.0)

        mc = run_monte_carlo_permutation([15.0, -10.0, 20.0, -10.0, 12.0, -10.0, 18.0], iterations=500, seed=7)
        self.assertEqual(mc["iterations"], 500)
        self.assertGreaterEqual(mc["p95_max_dd_pct"], mc["median_max_dd_pct"])

    def test_rolling_wfa_cycles_and_2x_cost_stress(self):
        # Генерируем 1200 15m баров (~12.5 дней) с простым детерминированным сигналом
        start_ms = 1_700_000_000_000
        bars = []
        px = 100.0
        for i in range(1200):
            nxt = px * (1.0 + 0.0012 * math.sin(i / 8.0) + 0.00015)
            bars.append(
                Candle(
                    timestamp=start_ms + i * 900_000,
                    open=px,
                    high=max(px, nxt) * 1.004,
                    low=min(px, nxt) * 0.996,
                    close=nxt,
                    volume=500.0,
                    turnover=500.0 * nxt,
                )
            )
            px = nxt

        def dummy_strat(sym: str, hist: list[Candle], ref_open: float):
            if len(hist) % 15 != 0:
                return None
            return TradeSignal(
                symbol=sym,
                strategy_id="TEST_STRAT",
                strategy_name="Test",
                timeframe="15",
                side="Buy",
                order_type="Market",
                entry_price=ref_open,
                stop_loss=ref_open * 0.99,
                take_profit_1=ref_open * 1.015,
                take_profit_2=ref_open * 1.015,
                base_winrate_est=0.5,
                rationale=["test"],
                invalidation_rule="sl",
                features={"max_hold_bars": 12.0},
            )

        bt = WalkForwardBacktester({})
        res = bt.run_rolling_wfa({"BTCUSDT": bars}, strategy_fn=dummy_strat, n_cycles=6, is_ratio=0.75, n_trials=2, mc_iterations=300)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["cycles"]), 6)
        self.assertGreater(res["oos_summary"]["total_trades"], 0)
        # При 2x издержках доходность строго хуже (или равна при 0 сделок)
        self.assertLessEqual(res["oos_stress_2x"]["net_return_pct"], res["oos_summary"]["net_return_pct"])

    def test_paginated_klines_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            c = BybitV5Client({}, allow_offline_fallback=False)
            start_ms = 1_700_000_000_000
            page1 = [Candle(start_ms + i * 900_000, 100, 101, 99, 100, 10, 1000) for i in range(1000, 2000)]
            page2 = [Candle(start_ms + i * 900_000, 100, 101, 99, 100, 10, 1000) for i in range(0, 1000)]
            calls = []

            def fake_get_klines(sym, interval="15", limit=200, start=None, end=None):
                calls.append(end)
                return page1 if len(calls) == 1 else page2

            c.get_klines = fake_get_klines
            out = c.get_klines_paginated("BTCUSDT", interval="15", days=15, end_ms=page1[-1].timestamp, cache_dir=Path(td))
            self.assertEqual(len(calls), 2)
            self.assertGreaterEqual(len(out), 1400)
            # Второй вызов берёт данные из кэша без HTTP-запросов
            out_cached = c.get_klines_paginated("BTCUSDT", interval="15", days=15, end_ms=page1[-1].timestamp, cache_dir=Path(td))
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(out_cached), len(out))


if __name__ == "__main__":
    unittest.main()

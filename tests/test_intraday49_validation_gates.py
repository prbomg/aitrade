# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.26-intraday.49: Шлюзы 1–2 (Lookahead, Recursive, PSR/MinBTL/PBO, BTC Beta/Alpha, Regimes, Red Flags, PSI/KS Drift, Shuffle Control)."""
from __future__ import annotations

import math
import random
import unittest
from typing import List

from aitrade.backtest.validation_gates import (
    compute_btc_beta_and_jensen_alpha,
    compute_feature_drift,
    compute_pbo_cpcv,
    compute_psr_and_min_btl,
    compute_regime_stress_breakdown,
    detect_overfitting_red_flags,
    run_lookahead_check,
    run_recursive_warmup_check,
)
from aitrade.exchange.bybit_v5 import Candle
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.strategies.booster_breakout import generate_booster_breakout_signal
from aitrade.strategies.donchian_intraday import generate_donchian_intraday_signal
from aitrade.strategies.donchian_tsmom import generate_donchian_tsmom_signal
from aitrade.strategies.funding_squeeze_momentum import generate_funding_squeeze_signal
from aitrade.strategies.keltner_trend_4h import generate_keltner_trend_4h_signal
from aitrade.strategies.orb_session_breakout import generate_orb_session_signal
from aitrade.strategies.regime_s1_s3 import generate_s1_s3_signal


def _make_synthetic_candles(n: int = 560, step_ms: int = 900_000, seed: int = 7) -> List[Candle]:
    rng = random.Random(seed)
    ts0 = 1_760_054_400_000
    px = 100.0
    out: List[Candle] = []
    for i in range(n):
        drift = 0.0005 if (i // 80) % 2 == 0 else -0.0002
        ret = drift + rng.gauss(0.0, 0.0045)
        o = px
        c = max(10.0, o * (1.0 + ret))
        h = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.002)))
        l = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.002)))
        vol = 1000.0 * (1.0 + abs(rng.gauss(0.0, 0.6)))
        out.append(Candle(ts0 + i * step_ms, round(o, 4), round(h, 4), round(l, 4), round(c, 4), round(vol, 2), round(vol * c, 2)))
        px = c
    return out


class TestIntraday49ValidationGates(unittest.TestCase):
    def test_lookahead_check_passes_all_core_and_demo_lab_strategies(self) -> None:
        bars_15m = _make_synthetic_candles(280, step_ms=900_000, seed=11)
        bars_1h = _make_synthetic_candles(220, step_ms=3_600_000, seed=12)
        bars_4h = _make_synthetic_candles(220, step_ms=14_400_000, seed=13)

        checks = [
            ("T1", bars_4h, lambda s, h, o: generate_donchian_tsmom_signal(s, h, reference_open_px=o), 90),
            ("T3", bars_4h, lambda s, h, o: generate_keltner_trend_4h_signal(s, h, reference_open_px=o), 50),
            ("B1", bars_15m, lambda s, h, o: generate_booster_breakout_signal(s, h, reference_open_px=o), 85),
            ("B2", bars_1h, lambda s, h, o: generate_donchian_intraday_signal(s, h, reference_open_px=o), 50),
            ("B4", bars_15m, lambda s, h, o: generate_orb_session_signal(s, h, rvol=1.8, reference_open_px=o), 85),
            ("B7", bars_1h, lambda s, h, o: generate_funding_squeeze_signal(s, h, funding_rate=-0.0005, reference_open_px=o), 50),
            ("S1", bars_15m, lambda s, h, o: generate_s1_s3_signal(s, h, reference_open_px=o), 85),
        ]
        for name, bars, fn, wu in checks:
            res = run_lookahead_check(bars, fn, symbol="SOLUSDT", warmup=wu, max_checks=25)
            self.assertTrue(res["passed"], f"Lookahead failed for {name}: {res['violations']}")
            self.assertGreater(res["checked_bars"], 0)

    def test_lookahead_check_catches_mutating_strategy(self) -> None:
        bars = _make_synthetic_candles(120, step_ms=900_000, seed=3)

        def bad_mutating_fn(sym, hist, ref_open):
            hist[-1] = Candle(hist[-1].timestamp, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0)
            return None

        res = run_lookahead_check(bars, bad_mutating_fn, warmup=60, max_checks=10)
        self.assertFalse(res["passed"])
        self.assertTrue(any("mutated" in v["reason"] for v in res["violations"]))

    def test_recursive_warmup_check_verifies_odd_windows(self) -> None:
        bars_4h = _make_synthetic_candles(560, step_ms=14_400_000, seed=21)
        res = run_recursive_warmup_check(
            bars_4h,
            lambda s, h, o: generate_donchian_tsmom_signal(s, h, reference_open_px=o),
            symbol="BTCUSDT",
            warmup_windows=(199, 299, 499),
        )
        self.assertEqual(res["windows_checked"], [199, 299, 499])
        self.assertTrue(res["passed"], f"Recursive warnings: {res['warnings']}")

    def test_psr_min_btl_and_pbo_distinguish_edge_from_noise(self) -> None:
        rng = random.Random(42)
        strong_rets = [0.0035 + rng.gauss(0.0, 0.006) for _ in range(180)]
        weak_rets = [-0.001 + rng.gauss(0.0, 0.012) for _ in range(180)]

        psr_strong = compute_psr_and_min_btl(strong_rets)
        psr_weak = compute_psr_and_min_btl(weak_rets)
        self.assertGreater(psr_strong["psr"], 0.95)
        self.assertIsNotNone(psr_strong["min_btl_days"])
        self.assertLess(psr_weak["psr"], 0.50)

        pbo_strong = compute_pbo_cpcv(strong_rets, n_blocks=8)
        pbo_weak = compute_pbo_cpcv(weak_rets, n_blocks=8)
        self.assertIsNotNone(pbo_strong)
        self.assertLess(pbo_strong, 0.25)
        self.assertGreater(pbo_weak, 0.50)

    def test_btc_beta_and_jensen_alpha_decomposition(self) -> None:
        rng = random.Random(99)
        btc_rets = [rng.gauss(0.001, 0.02) for _ in range(120)]
        # Чистая бета (1.0 * BTC) без альфы
        pure_beta = compute_btc_beta_and_jensen_alpha(btc_rets, btc_rets)
        self.assertAlmostEqual(pure_beta["btc_beta"], 1.0, places=2)
        self.assertAlmostEqual(pure_beta["jensen_alpha_ann_pct"], 0.0, places=1)

        # Рыночно-нейтральная альфа (+0.1% в день независимо от BTC)
        alpha_rets = [0.001 + 0.0 * b + rng.gauss(0.0, 0.0005) for b in btc_rets]
        neutral = compute_btc_beta_and_jensen_alpha(alpha_rets, btc_rets)
        self.assertLess(abs(neutral["btc_beta"]), 0.15)
        self.assertGreater(neutral["jensen_alpha_ann_pct"], 25.0)

    def test_regime_stress_breakdown_and_overfitting_red_flags(self) -> None:
        base_day = 20000
        btc_daily = {base_day + d: (0.015 if d < 20 else -0.015) for d in range(40)}
        trades = [
            ((base_day + 10) * 86_400_000, 1.5, 1.2, "T1", (base_day + 11) * 86_400_000, "BTCUSDT", 0.01),
            ((base_day + 30) * 86_400_000, -1.0, -1.0, "T1", (base_day + 31) * 86_400_000, "BTCUSDT", 0.01),
        ]
        reg = compute_regime_stress_breakdown(trades, btc_daily)
        self.assertEqual(reg["BULL"]["trades"], 1)
        self.assertEqual(reg["BEAR"]["trades"], 1)

        flags = detect_overfitting_red_flags(
            {"total_trades": 15, "win_rate_pct": 88.0, "profit_factor": 5.5, "net_return_pct": 45.0, "max_drawdown_pct": 0.2},
            {"wfe": 0.30, "pbo": 0.65, "psr": 0.70, "oos_stress_2x": {"avg_r_multiple": -0.05}},
            n_params=10,
        )
        self.assertGreaterEqual(len(flags), 6)

    def test_feature_drift_psi_ks_and_shuffle_noise_control(self) -> None:
        rng = random.Random(123)
        x_ref = [[rng.gauss(0.0, 1.0), rng.gauss(0.0, 1.0)] for _ in range(120)]
        x_same = [[rng.gauss(0.0, 1.0), rng.gauss(0.0, 1.0)] for _ in range(120)]
        x_shifted = [[rng.gauss(2.2, 1.0), rng.gauss(-1.8, 1.0)] for _ in range(120)]

        d_ok = compute_feature_drift(x_ref, x_same)
        d_alert = compute_feature_drift(x_ref, x_shifted)
        self.assertEqual(d_ok["status"], "OK")
        self.assertEqual(d_alert["status"], "DRIFT_ALERT")
        self.assertGreater(d_alert["max_psi"], 0.25)

        # Шумовой тест (Shuffle Control): на перемешанных метках калибратор не должен давать ложную уверенность
        ml = WalkForwardMetaLabeler({"exchange": {"mode": "demo"}})
        y_shuf = [float(i % 2) for i in range(120)]
        rng.shuffle(y_shuf)
        intervals = [(i * 900_000, (i + 4) * 900_000) for i in range(120)]
        probs = [ml.fit_predict_calibrated_prob(x_ref, y_shuf, intervals, x_same[k])[0] for k in range(5)]
        mean_p = sum(probs) / len(probs)
        self.assertTrue(0.30 <= mean_p <= 0.70, f"Shuffled labels produced suspicious mean prob: {mean_p}")


if __name__ == "__main__":
    unittest.main()

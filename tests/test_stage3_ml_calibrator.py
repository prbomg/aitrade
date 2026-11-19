import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from aitrade.booster.ml_calibrator import AdaptiveMLCalibrator
from aitrade.exchange.bybit_v5 import Candle
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler, purged_kfold_splits
from aitrade.strategies.base import TradeSignal


class Stage3MlAndCalibratorTests(unittest.TestCase):
    def test_purged_kfold_removes_overlapping_intervals_and_applies_embargo(self):
        # 50 сделок с длительностью 3 шага каждая
        intervals = [(i * 1000, (i + 3) * 1000) for i in range(50)]
        splits = purged_kfold_splits(intervals, n_splits=5, embargo_pct=0.04)
        self.assertEqual(len(splits), 5)
        for tr, te in splits:
            t_min = min(intervals[i][0] for i in te)
            t_max = max(intervals[i][1] for i in te)
            te_end = max(te)
            for idx in tr:
                s, e = intervals[idx]
                # Нет перекрытия по времени
                self.assertFalse(e >= t_min and s <= t_max)
                # Embargo минимум 2 шага после конца теста (4% от 50 = 2)
                if idx > te_end:
                    self.assertGreaterEqual(idx, te_end + 3)

    def test_meta_labeler_warming_up_without_fake_base_winrate(self):
        ml = WalkForwardMetaLabeler({})
        # 50 баров -> меньше 100 сэмплов -> статус WARMING_UP
        start_ms = 1_700_000_000_000
        bars = [Candle(start_ms + i * 900_000, 100, 100.4, 99.6, 100, 100, 10000) for i in range(50)]
        sig = TradeSignal(
            symbol="BTCUSDT",
            strategy_id="S1_TREND_RETEST",
            strategy_name="S1",
            timeframe="15",
            side="Buy",
            order_type="Limit",
            entry_price=100.0,
            stop_loss=98.8,
            take_profit_1=102.2,
            take_profit_2=103.6,
            base_winrate_est=0.95,  # Намеренно завышенная константа — НЕ должна влиять!
            rationale=["test"],
            invalidation_rule="sl",
            features={"post_only": 1.0},
        )
        ctx = NS(
            candles_15m=bars,
            micro=NS(smoothed_obi=0.5, vamp_divergence_bps=1.0, liquidity_behavior_index=0.9),
            screener=NS(rvol=2.0, spread_pct=0.02),
        )
        out = ml.evaluate_signal(sig, ctx)
        self.assertEqual(out.features["ml_calibration_status"], "WARMING_UP")
        self.assertNotAlmostEqual(out.ml_win_probability, 0.95, places=1)
        self.assertTrue(out.passed_ml_filter)

    def test_meta_labeler_calibrated_classifier_separates_good_vs_bad(self):
        ml = WalkForwardMetaLabeler({})
        # Синтетический разделимый датасет из 120 наблюдений
        X = []
        y = []
        intervals = []
        for i in range(120):
            is_win = 1.0 if i % 2 == 0 else 0.0
            vec = [1.2 if is_win else -1.2, 0.6 if is_win else -0.6, 0.8 if is_win else 0.2, 0.7 if is_win else -0.7]
            X.append(vec)
            y.append(is_win)
            intervals.append((i * 10000, i * 10000 + 5000))
        p_good, backend = ml.fit_predict_calibrated_prob(X, y, intervals, [1.2, 0.6, 0.8, 0.7])
        p_bad, _ = ml.fit_predict_calibrated_prob(X, y, intervals, [-1.2, -0.6, 0.2, -0.7])
        self.assertGreater(p_good, 0.70)
        self.assertLess(p_bad, 0.30)
        self.assertIn(backend, ("pure_python_platt_logreg", "sklearn_logreg_calibrated", "lightgbm_calibrated"))

    def test_calibrator_no_eternal_quarantine_and_auto_exit(self):
        with tempfile.TemporaryDirectory() as td:
            cal = AdaptiveMLCalibrator(Path(td))
            now = 1_700_000_000.0
            # 1) 3 стопа подряд при выборке < 20 -> НЕ должно быть QUARANTINE!
            small_losses = [
                {"strategy_id": "CORE:S1_TREND_RETEST", "closedPnl": -5.0, "updatedTime": int((now - 100 + i) * 1000)}
                for i in range(3)
            ]
            cal.calibrate_from_closed_pnl(small_losses, force=True, now_ts=now)
            mod_3 = cal.get_strategy_modifier("S1_TREND_RETEST")
            self.assertEqual(mod_3["status"], "NORMAL")
            self.assertEqual(mod_3["weight"], 1.0)

            # 2) 25 сделок с PF < 0.75 -> QUARANTINE (0.0x) на 48ч
            bad_25 = [
                {"strategy_id": "CORE:S1_TREND_RETEST", "closedPnl": (3.0 if i % 5 == 0 else -5.0), "updatedTime": int((now - 500 + i) * 1000)}
                for i in range(25)
            ]
            cal.calibrate_from_closed_pnl(bad_25, force=True, now_ts=now)
            mod_q = cal.get_strategy_modifier("S1_TREND_RETEST")
            self.assertEqual(mod_q["status"], "QUARANTINE")
            self.assertEqual(mod_q["weight"], 0.0)

            # 3) Через 48ч + 1 сек -> автовыход в COOLING (0.50x)
            cal.calibrate_from_closed_pnl(bad_25, force=False, now_ts=now + 48 * 3600 + 10)
            mod_exit = cal.get_strategy_modifier("S1_TREND_RETEST")
            self.assertEqual(mod_exit["status"], "COOLING")
            self.assertEqual(mod_exit["weight"], 0.50)

            # 4) Возврат в карантин и автовыход через 10 теневых сделок с PF >= 1.10
            cal.calibrate_from_closed_pnl(bad_25, force=True, now_ts=now + 50 * 3600)
            self.assertEqual(cal.get_strategy_modifier("S1_TREND_RETEST")["status"], "QUARANTINE")
            for i in range(10):
                cal.record_shadow_trade("S1_TREND_RETEST", 4.0 if i % 2 == 0 else -2.0)
            cal.calibrate_from_closed_pnl(bad_25, force=True, now_ts=now + 50 * 3600 + 60)
            self.assertEqual(cal.get_strategy_modifier("S1_TREND_RETEST")["status"], "COOLING")


if __name__ == "__main__":
    unittest.main()

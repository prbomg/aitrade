import tempfile
import unittest
from pathlib import Path

from aitrade.booster.ml_calibrator import AdaptiveMLCalibrator
from aitrade.execution.limit_chaser import SmartLimitChaser


class TestIntraday29CalibratorAndChaser(unittest.TestCase):
    def test_ml_calibrator_ignores_legacy_unattributed_losses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cal = AdaptiveMLCalibrator(Path(td))
            legacy_losses = [
                {"strategy_id": "UNATTRIBUTED", "closedPnl": -1.5, "updatedTime": 1_700_000_000_000 + i * 1000}
                for i in range(120)
            ]
            summary = cal.calibrate_from_closed_pnl(legacy_losses, force=True)
            self.assertEqual(summary["portfolio_stats"]["total_trades"], 0)
            self.assertEqual(summary["recommended_ml_threshold_pct"], 58.0)
            names = {s["strategy_name"] for s in summary["strategy_list"]}
            self.assertIn("T3_KELTNER_TREND_4H", names)
            self.assertIn("B6_EMA_PULLBACK_TREND", names)
            self.assertIn("C3_ENSEMBLE_VOTE_2OF3", names)

    def test_limit_chaser_uses_fast_3s_post_only_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ch = SmartLimitChaser(None, None, Path(td))
            self.assertEqual(ch.max_attempts, 2)
            self.assertAlmostEqual(ch.step_wait_sec, 1.5, places=2)


if __name__ == "__main__":
    unittest.main()

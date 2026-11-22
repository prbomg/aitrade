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

    def test_guard_cycle_with_positions_and_zero_open_orders_does_not_raise_unbound_local(self) -> None:
        from types import SimpleNamespace
        from aitrade.dashboard.live_server import ControlCenterState

        st = ControlCenterState.__new__(ControlCenterState)
        st.split_tp_enabled = True
        st.regime_guard = SimpleNamespace(time_stop_enabled=True, time_stop_minutes=45, time_stop_band_pct=0.25)
        st.logs = []
        st.add_log = lambda msg: st.logs.append(msg)  # type: ignore

        fake_ctrl = SimpleNamespace(
            spm=SimpleNamespace(split_tp_enabled=True, audit_and_protect_open_positions=lambda: []),
            get_open_orders=lambda: [],
            get_positions=lambda: [
                {
                    "symbol": "SOLUSDT",
                    "side": "Buy",
                    "size": "1.0",
                    "avgPrice": "150.0",
                    "markPrice": "150.1",
                    "stopLoss": "147.0",
                    "positionIdx": 0,
                    "entry_intent_id": "at-test-1",
                    "strategy_id": "CORE:T1_DONCHIAN_TSMOM",
                    "opened_ms": 0,
                }
            ],
            get_closed_pnl=lambda: [],
            ledger=SimpleNamespace(active_intent=lambda s, sd, sz: {"exit_plan": {"max_hold_minutes": 14400}}),
        )
        syms = st._run_guard_cycle_for_controller(fake_ctrl)
        self.assertEqual(syms, {"SOLUSDT"})


if __name__ == "__main__":
    unittest.main()

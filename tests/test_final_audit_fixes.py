import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from aitrade.dashboard.live_server import ControlCenterState


class FinalAuditFixesTests(unittest.TestCase):
    def test_valid_core_admits_warming_up_autotrade_and_blocks_booster_strategies(self):
        s = ControlCenterState.__new__(ControlCenterState)
        s._lock = threading.RLock()
        s.entry_paused = threading.Event()
        s.settings = {"universe": {"core_pairs": ["BTCUSDT", "ETHUSDT"]}}
        s.ws_hub = NS(get_summary=lambda: {})
        s.regime_guard = NS(
            update_btc_regime=lambda _: {},
            get_sector=lambda _: "MAJORS",
            can_open_position=lambda *a: (True, "ok"),
        )
        s.x_radar = NS(refresh_feed=lambda: {}, get_coin_modifier=lambda *a: {"allowed": True, "tag": "", "score_bonus": 0})
        s.oi_scanner = NS(scan_squeeze_setups=lambda *a: [])

        sig_s3_warm = {
            "symbol": "ETHUSDT",
            "strategy_id": "S3_VWAP_RANGE",
            "strategy_name": "S3",
            "side": "Buy",
            "order_type": "Limit",
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit_1": 101.5,
            "take_profit_2": 101.5,
            "position_qty": 2.0,
            "ml_win_probability": 0.50,  # WARMING_UP честная вероятность 0.50 (< 0.60)
            "passed_ml_filter": True,
            "debate_verdict": "APPROVED",
            "features": {"ml_calibration_status": "WARMING_UP", "post_only": 1.0},
        }
        sig_b1 = {
            "symbol": "BTCUSDT",
            "strategy_id": "B1_SQUEEZE_BREAKOUT",
            "strategy_name": "B1",
            "side": "Buy",
            "order_type": "Limit",
            "entry_price": 100.0,
            "stop_loss": 98.5,
            "take_profit_1": 103.0,
            "take_profit_2": 105.0,
            "position_qty": 2.0,
            "ml_win_probability": 0.80,
            "passed_ml_filter": True,
            "debate_verdict": "APPROVED",
            "features": {"ml_calibration_status": "pure_python_platt_logreg", "post_only": 1.0},
        }
        s.engine = NS(scan_market=lambda **kw: {"approved_signals": [sig_b1, sig_s3_warm], "rejected_signals": []})
        s.bybit = NS(get_positions=lambda: [], get_closed_pnl=lambda: [], place_order=Mock(return_value={"ok": True, "qty": "2.0"}))
        s.ml_calibrator = NS(
            state={"enabled": False},
            calibrate_from_closed_pnl=lambda _: {},
            get_strategy_modifier=lambda _: {"weight": 1.0, "status": "NORMAL", "badge": "", "prob_adj": 0},
            register_entry=Mock(),
        )
        s.booster = NS(compute_metrics=lambda *a: {})
        s.core_max_positions = 3
        s.core_min_prob_pct = 60.0
        s.max_leverage = 3
        s.split_tp_enabled = True
        s.add_log = Mock()
        s.mark_position_source = Mock()

        s._run_scan(trade_booster=False, trade_core=True)
        # B1_SQUEEZE_BREAKOUT НЕ должен исполняться в контуре Автотрейдинга, а S3 в WARMING_UP — должен!
        self.assertEqual(s.bybit.place_order.call_count, 1)
        self.assertEqual(s.bybit.place_order.call_args.args[0], "ETHUSDT")
        self.assertEqual(s.bybit.place_order.call_args.kwargs["strategy_id"], "CORE:S3_VWAP_RANGE")

    def test_dead_mans_switch_covers_isolated_booster_controller(self):
        s = ControlCenterState.__new__(ControlCenterState)
        s.started_at = time.time() - 120.0
        s.ws_hub = NS(get_summary=lambda: {"connected": False, "last_msg_age_sec": 45.0})
        s.bybit = NS(
            get_open_orders=lambda: [{"symbol": "ETHUSDT", "orderId": "auto-1", "reduceOnly": False}],
            cancel_order=Mock(return_value={"ok": True}),
        )
        s.booster_bybit = NS(
            get_open_orders=lambda: [{"symbol": "BTCUSDT", "orderId": "boost-1", "reduceOnly": False}],
            cancel_order=Mock(return_value={"ok": True}),
        )
        s.add_log = Mock()

        cancelled = s._check_dead_mans_switch()
        self.assertEqual(cancelled, 2)
        s.bybit.cancel_order.assert_called_once_with("ETHUSDT", "auto-1")
        s.booster_bybit.cancel_order.assert_called_once_with("BTCUSDT", "boost-1")

    def test_autotrade_3_level_kill_switch_and_health_version(self):
        s = ControlCenterState.__new__(ControlCenterState)
        s.autotrade_hwm = 1000.0
        self.assertEqual(s.check_autotrade_drawdown(960.0), (1.0, False))  # -4%
        self.assertEqual(s.check_autotrade_drawdown(910.0), (0.5, False))  # -9% -> риск вдвое
        self.assertEqual(s.check_autotrade_drawdown(840.0), (0.0, True))   # -16% -> стоп

        s.release_manifest = {"version": "2026.11.19-intraday.5", "build_sha256": "abc"}
        s.scan_status = {"running": False, "last_attempt": 0, "failed_cycles": 0, "last_error": ""}
        s._autopilot_thread = NS(is_alive=lambda: True)
        s.entry_paused = threading.Event()
        s._snapshot_ts = time.time()
        s._snapshot_error = ""
        s.guard_status = {"heartbeat": time.time(), "last_error": ""}
        self.assertEqual(s.health()["version"], "2026.11.19-intraday.5")


if __name__ == "__main__":
    unittest.main()

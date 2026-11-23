import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.booster.env_manager import BybitEnvManager
from aitrade.config import BASE_DIR
from aitrade.execution.limit_chaser import SmartLimitChaser
from aitrade.execution.smart_position_manager import SmartPositionManager


class Intraday38Audit2FixesTests(unittest.TestCase):
    def test_spm_load_guard_returns_saved_tp1_record(self):
        with tempfile.TemporaryDirectory() as td:
            gp = Path(td) / "position_guard.json"
            key = "https://api-demo.bybit.com:BTCUSDT:0:at-123"
            gp.write_text(json.dumps({key: {"tp1_order": "ord-tp1-99"}}), encoding="utf-8")
            spm = SmartPositionManager("k", "s", guard_path=gp)
            loaded = spm.load_guard()
            self.assertEqual(loaded[key]["tp1_order"], "ord-tp1-99")

    def test_limit_chaser_partial_post_only_does_not_turn_uncertain_on_cap_skip(self):
        with tempfile.TemporaryDirectory() as td:
            spm = MagicMock()
            spm.ledger = None
            spm.get_spec.return_value = {"qty_step": 0.01, "min_qty": 0.01, "tick_size": 0.01}
            spm.fmt_step.side_effect = lambda v, s, round_down=True: f"{float(v):.2f}"
            # PostOnly order partially fills 0.40 out of 1.00, then cancels cleanly
            spm.signed_post.return_value = {"retCode": 0, "result": {"orderId": "po-1"}}
            spm.signed_get.return_value = {
                "result": {"list": [{"orderId": "po-1", "orderStatus": "PartiallyFilledCanceled", "cumExecQty": "0.40"}]}
            }
            ws = MagicMock()
            # First call (PostOnly): bid=100, ask=100.01; Second call (fallback): ask=100.90 so cap=101.05 > tp=101.00
            ws.get_bbo.side_effect = [(100.0, 100.01), (100.80, 100.90)]
            chaser = SmartLimitChaser(spm, ws, Path(td))
            chaser.step_wait_sec = 0.0
            chaser.max_attempts = 1
            chaser.max_chase_drift_pct = 2.0

            res = chaser.execute_chased_entry(
                "ETHUSDT", "Buy", 1.0, 100.0, take_profit=101.0, stop_loss=99.0, order_link_id="at-part1"
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "PARTIAL")
            self.assertEqual(float(res["qty"]), 0.4)

    def test_env_manager_save_keys_returns_error_on_disk_failure(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = BybitEnvManager(Path(td), "demo_k", "demo_s")
            with patch("aitrade.booster.env_manager.atomic_json", side_effect=OSError("disk full")):
                r1 = mgr.save_keys("mainnet", "mk123456", "ms123456")
                r2 = mgr.save_subaccount_keys("booster", "demo", "bk123456", "bs123456")
            self.assertFalse(r1["ok"])
            self.assertIn("disk full", r1["retMsg"])
            self.assertFalse(r2["ok"])
            self.assertIn("disk full", r2["retMsg"])

    def test_frontend_uses_kairos_header_and_no_pos_pyramid_route(self):
        js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")
        login_html = (BASE_DIR / "login.html").read_text(encoding="utf-8")
        srv = (BASE_DIR / "aitrade" / "dashboard" / "live_server.py").read_text(encoding="utf-8")
        self.assertIn('"X-Requested-With": "KAIROS"', js)
        self.assertIn('"X-Requested-With": "KAIROS"', login_html)
        self.assertNotIn('"/api/pos_pyramid"', srv)
        self.assertNotIn("data-action=ws_quick", js)
        self.assertNotIn("data-action=exec_sig", js)
        self.assertNotIn("data-action=booster_exec", js)

    def test_b4_orb_window_helper_and_idle_reason(self):
        import datetime
        from aitrade.strategies.orb_session_breakout import is_in_orb_session_window

        def ts(hour, minute=15):
            return int(datetime.datetime(2026, 11, 23, hour, minute, tzinfo=datetime.timezone.utc).timestamp() * 1000)

        for active_h in (1, 4, 8, 11, 14, 17):
            self.assertTrue(is_in_orb_session_window(ts(active_h)), f"Expected active at {active_h}:15 UTC")
        for idle_h in (0, 5, 7, 12, 13, 18, 23):
            self.assertFalse(is_in_orb_session_window(ts(idle_h)), f"Expected idle at {idle_h}:15 UTC")

        eng_src = (BASE_DIR / "aitrade" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("is_in_orb_session_window(", eng_src)
        self.assertIn("Вне окна ORB (ждёт 01:00–04:59 / 08:00–11:59 / 14:00–17:59 UTC)", eng_src)


if __name__ == "__main__":
    unittest.main()

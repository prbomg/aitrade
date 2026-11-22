import io
import json
import threading
import unittest
from unittest.mock import MagicMock

from aitrade.config import BASE_DIR
from aitrade.dashboard.live_server import ControlCenterState, create_wsgi_app


class Intraday34AuditFixesTests(unittest.TestCase):
    def _make_state(self):
        st = ControlCenterState.__new__(ControlCenterState)
        st.autopilot_booster_enabled = True
        st.autopilot_core_enabled = True
        st.autopilot_interval_sec = 300
        st.core_max_positions = 4
        st.core_min_prob_pct = 58.0
        st._last_auto_ts = 100.0
        st.entry_paused = threading.Event()
        st.entry_pause_reason = ""
        st.entry_paused_at = 0.0
        st.guard_status = {"heartbeat": 1.0, "last_success": 1.0, "last_error": ""}
        st.logs = []
        st.add_log = MagicMock()
        st.save_persisted_state = MagicMock()
        return st

    def test_emergency_stop_handles_account_failure_and_preserves_sl_on_partial_exit(self):
        st = self._make_state()

        # Контур 1: падает на get_open_orders, но закрывает позицию частично (остаток остаётся со SL)
        ctrl1 = MagicMock()
        ctrl1.get_open_orders.side_effect = RuntimeError("timeout on account 1")
        pos1 = {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1", "positionIdx": 0, "stopLoss": "60000"}
        ctrl1.get_positions.side_effect = [[pos1], [pos1]]
        ctrl1.close_position.return_value = {"ok": True}

        # Контур 2: успешно отменяет ТОЛЬКО входной ордер (не трогая SL до выхода), затем закрывает позицию
        ctrl2 = MagicMock()
        entry_ord = {"symbol": "ETHUSDT", "orderId": "e1", "reduceOnly": False, "stopOrderType": ""}
        sl_ord = {"symbol": "ETHUSDT", "orderId": "sl1", "reduceOnly": True, "stopOrderType": "StopLoss"}
        ctrl2.get_open_orders.side_effect = [[entry_ord, sl_ord], [sl_ord]]
        pos2 = {"symbol": "ETHUSDT", "side": "Buy", "size": "1.0", "positionIdx": 0, "stopLoss": "2900"}
        ctrl2.get_positions.side_effect = [[pos2], []]
        ctrl2.cancel_order.return_value = {"ok": True}
        ctrl2.close_position.return_value = {"ok": True}

        st.bybit = ctrl1
        st.booster_bybit = ctrl2

        rep = st.execute_emergency_stop()
        self.assertFalse(st.autopilot_booster_enabled)
        self.assertFalse(st.autopilot_core_enabled)
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(rep["status"], "ERROR")
        self.assertEqual(len(rep["accounts"]), 2)
        # Ошибка на ctrl1 не прервала обработку ctrl2
        self.assertEqual(rep["accounts"][1]["entry_orders_cancelled"], 1)
        self.assertEqual(rep["accounts"][1]["close_submitted"], 1)
        self.assertEqual(rep["accounts"][1]["remaining_positions"], 0)
        # На ctrl1 осталась позиция, защищённая SL -> unprotected_positions пуст
        self.assertEqual(rep["accounts"][0]["remaining_positions"], 1)
        self.assertEqual(rep["accounts"][0]["unprotected_positions"], [])
        # Первый cancel_order на ctrl2 был вызван ТОЛЬКО для входного ордера e1 (до закрытия)
        self.assertEqual(ctrl2.cancel_order.call_args_list[0].args, ("ETHUSDT", "e1"))

    def test_toggle_off_does_not_clear_protective_guard_pause(self):
        st = self._make_state()
        st.entry_paused.set()
        st.entry_pause_reason = "Ошибка Smart Guard: timeout"
        st.guard_status["last_error"] = "timeout"

        app = create_wsgi_app(st)
        body = json.dumps({"mode": "toggle_booster"}).encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/strategy_control",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_X_REQUESTED_WITH": "AItrade",
            "wsgi.input": io.BytesIO(body),
        }
        status_holder = []
        raw = b"".join(app(environ, lambda s, h: status_holder.append(s)))
        res = json.loads(raw.decode("utf-8"))
        self.assertTrue(res["ok"])
        self.assertFalse(res["booster_enabled"])
        self.assertTrue(st.entry_paused.is_set())
        self.assertIn("Ошибка Smart Guard", st.entry_pause_reason)

    def test_ui_has_mobile_overflow_guard_and_reusable_audio_context(self):
        html = (BASE_DIR / "dashboard_report.html").read_text(encoding="utf-8")
        js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")
        self.assertIn("overflow-x:clip", html)
        self.assertIn("minmax(min(260px,100%),1fr)", html)
        self.assertIn("window._audioUnlocked", js)
        self.assertIn("unlockAudioContext", js)
        self.assertIn("st.order_counts", js)
        self.assertIn("st.effective_limits", js)
        self.assertIn("st.pnl_summary", js)


if __name__ == "__main__":
    unittest.main()

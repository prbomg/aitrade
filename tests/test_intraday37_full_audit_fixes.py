import inspect
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.config import BASE_DIR
from aitrade.dashboard.live_server import KairosSessionAuth, create_wsgi_app
from aitrade.dashboard.report_generator import generate_html_dashboard
from aitrade.storage import Ledger


def _call_wsgi(app, method="GET", path="/", body=None, headers=None):
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json" if body is not None else "",
        "HTTP_HOST": "127.0.0.1:8765",
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(raw),
    }
    if headers:
        for k, v in headers.items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
    captured = {}

    def start_response(status, resp_headers):
        captured["status"] = status
        captured["headers"] = dict(resp_headers)

    chunks = app(environ, start_response)
    return captured["status"], captured["headers"], b"".join(chunks)


class Intraday37FullAuditFixesTests(unittest.TestCase):
    def setUp(self):
        self.state = MagicMock()
        self.state.health.return_value = {"ok": True, "version": "2026.11.22-intraday.37"}
        self.state.cached_state.return_value = {"ok": True}
        self.state.release_manifest = {"version": "2026.11.22-intraday.37"}
        self.state.bybit = MagicMock()
        self.state.booster_bybit = MagicMock()
        self.state.bybit.close_position.return_value = {"ok": True, "account": "autotrade"}
        self.state.booster_bybit.close_position.return_value = {"ok": True, "account": "booster"}
        self.state.bybit.cancel_order.return_value = {"ok": True, "account": "autotrade"}
        self.state.booster_bybit.cancel_order.return_value = {"ok": True, "account": "booster"}

    def test_p0_rate_limit_cannot_be_bypassed_via_spoofed_xff_first_ip(self):
        auth = KairosSessionAuth()
        env_base = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_REAL_IP": "203.0.113.10"}
        for i in range(8):
            env = dict(env_base, HTTP_X_FORWARDED_FOR=f"1.2.3.{i}, 203.0.113.10")
            auth.record_failure(env)
        self.assertTrue(auth.is_rate_limited(dict(env_base, HTTP_X_FORWARDED_FOR="9.9.9.9, 203.0.113.10")))

        # Without X-Real-IP, uses last X-Forwarded-For entry (proxy-appended client IP)
        auth2 = KairosSessionAuth()
        for i in range(8):
            auth2.record_failure({"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": f"10.0.0.{i}, 198.51.100.5"})
        self.assertTrue(auth2.is_rate_limited({"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "8.8.8.8, 198.51.100.5"}))

    def test_p1_multi_controller_routing_for_position_and_order_actions(self):
        with patch.dict(os.environ, {"KAIROS_AUTH_USER": "", "KAIROS_AUTH_PASSWORD": ""}, clear=False):
            app = create_wsgi_app(self.state)
            st, _, body = _call_wsgi(
                app,
                "POST",
                "/api/close_position",
                body={"symbol": "BTCUSDT", "side": "Buy", "size": "0.1", "position_idx": 0, "account": "booster"},
                headers={"X-Requested-With": "KAIROS"},
            )
            self.assertTrue(st.startswith("200"))
            self.state.booster_bybit.close_position.assert_called_once()
            self.state.bybit.close_position.assert_not_called()

            st2, _, _ = _call_wsgi(
                app,
                "POST",
                "/api/cancel_order",
                body={"symbol": "ETHUSDT", "order_id": "ord-1", "account": "booster"},
                headers={"X-Requested-With": "KAIROS"},
            )
            self.assertTrue(st2.startswith("200"))
            self.state.booster_bybit.cancel_order.assert_called_once_with("ETHUSDT", "ord-1")

    def test_p1_internal_exception_returns_500_without_leaking_details(self):
        self.state.bybit.cancel_order.side_effect = RuntimeError("sensitive internal traceback detail")
        with patch.dict(os.environ, {"KAIROS_AUTH_USER": "", "KAIROS_AUTH_PASSWORD": ""}, clear=False):
            app = create_wsgi_app(self.state)
            st, _, body = _call_wsgi(
                app,
                "POST",
                "/api/cancel_order",
                body={"symbol": "BTCUSDT", "order_id": "ord-2"},
                headers={"X-Requested-With": "KAIROS"},
            )
            self.assertTrue(st.startswith("500"))
            payload = json.loads(body)
            self.assertNotIn("sensitive internal", payload.get("retMsg", ""))
            self.assertEqual(payload.get("retMsg"), "Внутренняя ошибка сервера")

    def test_p1_report_generator_does_not_default_to_dashboard_report_html(self):
        sig = inspect.signature(generate_html_dashboard)
        default_out = Path(sig.parameters["output_path"].default)
        self.assertNotEqual(default_out.name, "dashboard_report.html")

    def test_p1_p2_frontend_401_account_escaping_and_login_privacy(self):
        js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")
        login_html = (BASE_DIR / "login.html").read_text(encoding="utf-8")
        self.assertIn('if (r.status === 401) { redirectToLogin(); return; }', js)
        self.assertIn('data-account=\\"', js)
        self.assertIn('closeStrategyDetail(true, true)', js)
        self.assertNotIn("KAIROS_AUTH_USER", login_html)
        self.assertNotIn("KAIROS_AUTH_PASSWORD", login_html)

    def test_p2_ledger_connect_closes_sqlite_connection(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "test.sqlite3")
            with ledger.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            # After exiting context manager, connection must be closed
            with self.assertRaises(Exception):
                conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()

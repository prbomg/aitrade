# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.26-intraday.48: UX-апгрейд, единый рейтинг, триангуляция и пауза стратегий из UI."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aitrade.config import BASE_DIR
from aitrade.dashboard.live_server import ControlCenterState, create_wsgi_app
from aitrade.execution.shadow_engine import LiveShadowEngine


def _post_json(app, path: str, body: dict) -> tuple[str, dict]:
    raw = json.dumps(body).encode("utf-8")
    env = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": path,
        "CONTENT_TYPE": "application/json",
        "CONTENT_LENGTH": str(len(raw)),
        "HTTP_X_REQUESTED_WITH": "KAIROS",
        "HTTP_HOST": "127.0.0.1:8765",
        "wsgi.input": io.BytesIO(raw),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    resp_bytes = b"".join(app(env, start_response))
    return captured["status"], json.loads(resp_bytes.decode("utf-8"))


class TestIntraday48UxAndToggle(unittest.TestCase):
    def test_api_strategy_toggle_pauses_and_resumes_strategy(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st.paused_strategies = []
        st.logs = []
        st.add_log = lambda msg: st.logs.insert(0, msg)
        st.save_persisted_state = MagicMock()
        app = create_wsgi_app(st)

        status, r1 = _post_json(app, "/api/strategy_toggle", {"strategy_id": "B4_ORB_SESSION_BREAKOUT", "paused": True})
        self.assertEqual(status, "200 OK")
        self.assertTrue(r1["ok"])
        self.assertTrue(r1["paused"])
        self.assertIn("B4_ORB_SESSION_BREAKOUT", st.paused_strategies)
        self.assertEqual(st.save_persisted_state.call_count, 1)

        status2, r2 = _post_json(app, "/api/strategy_toggle", {"strategy_id": "BOOSTER:B4_ORB_SESSION_BREAKOUT", "paused": False})
        self.assertEqual(status2, "200 OK")
        self.assertTrue(r2["ok"])
        self.assertFalse(r2["paused"])
        self.assertNotIn("B4_ORB_SESSION_BREAKOUT", st.paused_strategies)

        status3, r3 = _post_json(app, "/api/strategy_toggle", {"strategy_id": ""})
        self.assertEqual(status3, "400 Bad Request")
        self.assertFalse(r3["ok"])

    def test_shadow_engine_detail_includes_exit_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng = LiveShadowEngine(Path(td) / "shadow.sqlite3")
            t0 = 1_800_000_000.0
            eng.record_signals(
                [
                    {"strategy_id": "B4_ORB_SESSION_BREAKOUT", "symbol": "BTCUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 104.0, "signal_id": "sig-tp"},
                    {"strategy_id": "B4_ORB_SESSION_BREAKOUT", "symbol": "ETHUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 104.0, "signal_id": "sig-sl"},
                    {"strategy_id": "B4_ORB_SESSION_BREAKOUT", "symbol": "SOLUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 104.0, "signal_id": "sig-ts"},
                ],
                {"BTCUSDT": (99.99, 100.01), "ETHUSDT": (99.99, 100.01), "SOLUSDT": (99.99, 100.01)},
                now_ts=t0,
            )
            eng.update_open_positions({"BTCUSDT": (104.5, 104.6), "ETHUSDT": (97.5, 97.6), "SOLUSDT": (100.5, 100.6)}, now_ts=t0 + 600)
            eng.update_open_positions({"SOLUSDT": (100.5, 100.6)}, now_ts=t0 + 480 * 60 + 10)
            det = eng.get_strategy_detail("B4_ORB_SESSION_BREAKOUT")
            self.assertEqual(det["exit_breakdown"], {"TP": 1, "SL": 1, "TIME_STOP": 1, "OTHER": 0})

    def test_ui_contains_unified_leaderboard_reconciliation_and_separated_drawers(self) -> None:
        html = (BASE_DIR / "dashboard_report.html").read_text(encoding="utf-8")
        js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")
        for elem_id in (
            "drawer-manual",
            "btn-toggle-manual",
            "btn-close-manual",
            "unified-strat-tbody",
            "unified-filter-bar",
            "hist-filter-strat",
            "hist-filter-sym",
            "hist-hide-legacy",
            "btn-sd-toggle-pause",
            "sd-exit-breakdown-box",
            "sd-reconcile-tbody",
        ):
            self.assertIn(f'id="{elem_id}"', html)
            self.assertIn(elem_id, js)
        self.assertIn("/api/strategy_toggle", js)
        self.assertIn("fill_latency_ms", js)
        self.assertIn("exit_reason", js)
        self.assertNotIn("13 стратегий", html)


if __name__ == "__main__":
    unittest.main()

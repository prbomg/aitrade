# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.24-intraday.45: 4 фикса внешнего аудита intraday.44."""
from __future__ import annotations

import io
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aitrade.config import BASE_DIR
from aitrade.dashboard.live_server import (
    ControlCenterState,
    compute_portfolio_risk_usd,
    create_wsgi_app,
)


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


def _make_stub_state(booster_on: bool = False, core_on: bool = False) -> ControlCenterState:
    st = ControlCenterState.__new__(ControlCenterState)
    st.started_at = time.time() - 120.0
    st.smart_guard_enabled = True
    st.split_tp_enabled = True
    st.autopilot_booster_enabled = booster_on
    st.autopilot_core_enabled = core_on
    st.autopilot_interval_sec = 300
    st.core_max_positions = 4
    st.core_min_prob_pct = 58.0
    st._last_auto_ts = 0.0
    st.entry_paused = threading.Event()
    st.entry_pause_reason = ""
    st.entry_paused_at = 0.0
    st._entry_pause_source = ""
    st._guard_consecutive_errors = 0
    st._guard_consecutive_ok = 0
    st.guard_status = {"heartbeat": time.time(), "last_success": 0.0, "last_error": ""}
    st.position_opened_ts = {}
    st.logs = []
    st.add_log = lambda msg: st.logs.insert(0, msg)
    st.save_persisted_state = MagicMock()
    st.refresh_booster_snapshot = MagicMock()
    st.ws_hub = MagicMock()
    st.ws_hub.get_summary.return_value = {"connected": True, "last_msg_age_sec": 1.0}
    st.regime_guard = MagicMock()
    st.regime_guard.time_stop_enabled = False
    st.bybit = MagicMock()
    st.bybit.spm = MagicMock()
    st.bybit.spm.audit_and_protect_open_positions.return_value = []
    st.bybit.get_open_orders.return_value = []
    st.bybit.get_positions.return_value = []
    st.bybit.get_closed_pnl.return_value = []
    st.booster_bybit = st.bybit
    st.booster = MagicMock()
    st.booster.state = {"training_unlimited_mode": False}
    return st


class TestIntraday45Audit4Fixes(unittest.TestCase):
    def test_emergency_stop_final_cancel_failure_never_returns_confirmed(self) -> None:
        st = _make_stub_state(booster_on=True, core_on=True)
        # 1) Начальный список ордеров: пусто (или только защитный), 2) финальная чистка видит оставшийся ордер, 3) сверка после ошибки отмены всё ещё видит ордер
        st.bybit.get_open_orders.side_effect = [
            [],
            [{"symbol": "BTCUSDT", "orderId": "ord-stuck", "reduceOnly": False, "stopOrderType": ""}],
            [{"symbol": "BTCUSDT", "orderId": "ord-stuck", "reduceOnly": False, "stopOrderType": ""}],
        ]
        st.bybit.get_positions.side_effect = [[], [], []]
        st.bybit.cancel_order.return_value = {"ok": False, "retMsg": "exchange timeout"}

        res = st.execute_emergency_stop()
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "ERROR")
        acc = res["accounts"][0]
        self.assertFalse(acc["ok"])
        self.assertEqual(acc["remaining_orders"], 1)
        self.assertEqual(acc["remaining_entry_orders"], 1)
        self.assertTrue(any("Финальная отмена BTCUSDT" in e for e in acc["errors"]))

    def test_emergency_stop_rechecks_orders_even_if_cancel_claims_ok(self) -> None:
        st = _make_stub_state(booster_on=True, core_on=True)
        st.bybit.get_open_orders.side_effect = [
            [],
            [{"symbol": "ETHUSDT", "orderId": "ord-1", "reduceOnly": False, "stopOrderType": ""}],
            [{"symbol": "ETHUSDT", "orderId": "ord-1", "reduceOnly": False, "stopOrderType": ""}],
        ]
        st.bybit.get_positions.side_effect = [[], [], []]
        st.bybit.cancel_order.return_value = {"ok": True, "retMsg": "OK"}

        res = st.execute_emergency_stop()
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "PARTIAL")
        self.assertEqual(res["accounts"][0]["remaining_entry_orders"], 1)

    def test_manual_enable_cannot_clear_guard_pause_after_single_ok_cycle(self) -> None:
        st = _make_stub_state(booster_on=False, core_on=False)
        st.bybit.spm.audit_and_protect_open_positions.side_effect = RuntimeError("network blip")
        for _ in range(3):
            st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._entry_pause_source, "guard_transient")

        # 1-й успешный цикл Guard: last_error НЕ должен сбрасываться до 3 чистых циклов
        st.bybit.spm.audit_and_protect_open_positions.side_effect = None
        st.bybit.spm.audit_and_protect_open_positions.return_value = []
        st._run_smart_guard_tick()
        self.assertEqual(st._guard_consecutive_ok, 1)
        self.assertTrue(bool(st.guard_status["last_error"]))

        app = create_wsgi_app(st)
        status, resp = _post_json(app, "/api/strategy_control", {"mode": "toggle_core"})
        self.assertEqual(status, "200 OK")
        self.assertTrue(resp["core_enabled"])
        self.assertTrue(resp["entry_paused"])
        self.assertTrue(st.entry_paused.is_set())
        self.assertIn("защитная пауза Smart Guard сохранена", resp.get("retMsg", ""))

        # После 3-го успешного цикла Guard восстанавливается и автоснимает паузу
        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        st._run_smart_guard_tick()
        self.assertFalse(st.entry_paused.is_set())
        self.assertEqual(st.guard_status["last_error"], "")

    def test_critical_guard_pause_requires_3_ok_cycles_before_manual_enable(self) -> None:
        st = _make_stub_state(booster_on=False, core_on=False)
        st.bybit.spm.audit_and_protect_open_positions.return_value = [
            {"ok": False, "action": "MISSING_STOP_LOSS", "symbol": "BTCUSDT"}
        ]
        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._entry_pause_source, "guard_critical")

        # 1 чистый цикл — ручное включение всё ещё не снимает критическую паузу
        st.bybit.spm.audit_and_protect_open_positions.return_value = []
        st._run_smart_guard_tick()
        app = create_wsgi_app(st)
        _, resp1 = _post_json(app, "/api/strategy_control", {"mode": "toggle_booster"})
        self.assertTrue(resp1["entry_paused"])

        # Ещё 2 чистых цикла (итого 3): автоснятия нет, но ручное включение теперь разрешено
        st._run_smart_guard_tick()
        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._entry_pause_source, "guard_critical")

        _, resp2 = _post_json(app, "/api/strategy_control", {"mode": "enable_both"})
        self.assertFalse(resp2["entry_paused"])
        self.assertFalse(st.entry_paused.is_set())

    def test_compute_portfolio_risk_usd_includes_pending_entry_orders(self) -> None:
        positions = [
            {"symbol": "BTCUSDT", "avgPrice": "100000", "stopLoss": "99000", "size": "0.01"},
        ]
        open_orders = [
            # Активный входной лимитный ордер (должен учитываться)
            {"symbol": "ETHUSDT", "price": "3000", "stopLoss": "2940", "leavesQty": "0.5", "reduceOnly": False, "stopOrderType": ""},
            # Защитный ордер SL/TP (НЕ должен дублироваться в риске)
            {"symbol": "BTCUSDT", "price": "105000", "stopLoss": "0", "leavesQty": "0.01", "reduceOnly": True, "stopOrderType": "TakeProfit"},
        ]
        pos_only = 0.01 * (abs(100000.0 - 99000.0) + 100000.0 * 0.0054)
        ord_only = 0.5 * (abs(3000.0 - 2940.0) + 3000.0 * 0.0054)
        expected = pos_only + ord_only

        self.assertAlmostEqual(compute_portfolio_risk_usd(positions, open_orders, strict=False), expected, places=6)
        self.assertAlmostEqual(compute_portfolio_risk_usd(positions, open_orders, strict=True), expected, places=6)

        # В строгом режиме заявка без SL бросает ValueError
        bad_orders = [{"symbol": "SOLUSDT", "price": "200", "stopLoss": "0", "qty": "1", "reduceOnly": False}]
        with self.assertRaises(ValueError):
            compute_portfolio_risk_usd(positions, bad_orders, strict=True)

    def test_dashboard_shows_loading_placeholders_before_first_snapshot(self) -> None:
        html = (BASE_DIR / "dashboard_report.html").read_text(encoding="utf-8")
        js = (BASE_DIR / "dashboard_app.js").read_text(encoding="utf-8")
        self.assertIn('id="master-status-badge">⏳ Загрузка…<', html)
        self.assertIn('id="kpi-usdt">Загрузка…<', html)
        self.assertIn('id="kpi-pos-orders">Загрузка…<', html)
        self.assertIn('id="badge-booster-state">Загрузка…<', html)
        self.assertIn('id="badge-core-state">Загрузка…<', html)
        self.assertIn('id="deck-active-summary" style="color:#94a3b8">Загрузка состояния…<', html)
        self.assertIn("const hasSnapshot = Boolean(st.account)", js)


if __name__ == "__main__":
    unittest.main()

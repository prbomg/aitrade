# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.23-intraday.40: Smart Guard 3-strike порог и автовосстановление."""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aitrade.dashboard.live_server import ControlCenterState


def _make_stub_state(booster_on: bool = True, core_on: bool = True) -> ControlCenterState:
    st = ControlCenterState.__new__(ControlCenterState)
    st.started_at = time.time() - 120.0
    st.smart_guard_enabled = True
    st.split_tp_enabled = True
    st.autopilot_booster_enabled = booster_on
    st.autopilot_core_enabled = core_on
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
    return st


class TestIntraday40GuardAutoRecover(unittest.TestCase):
    def test_transient_errors_require_3_strikes_and_auto_recover_after_3_ok(self) -> None:
        st = _make_stub_state(booster_on=True, core_on=False)
        st.bybit.spm.audit_and_protect_open_positions.side_effect = RuntimeError("timeout")

        # 1-й и 2-й сбой: пауза НЕ ставится
        st._run_smart_guard_tick()
        self.assertFalse(st.entry_paused.is_set())
        self.assertEqual(st._guard_consecutive_errors, 1)

        st._run_smart_guard_tick()
        self.assertFalse(st.entry_paused.is_set())
        self.assertEqual(st._guard_consecutive_errors, 2)

        # 3-й сбой подряд: ставится временная автопауза
        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._entry_pause_source, "guard_transient")
        self.assertIn("timeout", st.entry_pause_reason)

        # Восстанавливаем связь: 1-й и 2-й успешный цикл ещё держат паузу
        st.bybit.spm.audit_and_protect_open_positions.side_effect = None
        st.bybit.spm.audit_and_protect_open_positions.return_value = []

        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._guard_consecutive_ok, 1)

        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._guard_consecutive_ok, 2)

        # 3-й успешный цикл подряд: автоснятие паузы
        st._run_smart_guard_tick()
        self.assertFalse(st.entry_paused.is_set())
        self.assertEqual(st.entry_pause_reason, "")
        self.assertEqual(st._entry_pause_source, "")
        self.assertTrue(any("автоматически сняты с паузы" in line for line in st.logs))

    def test_protection_error_transient_vs_critical_sl_immediate_hard_lock(self) -> None:
        st = _make_stub_state(booster_on=True, core_on=True)

        # Одиночный PROTECTION_ERROR (временный сбой API при выставлении защиты) не паузит сразу
        st.bybit.spm.audit_and_protect_open_positions.return_value = [
            {"ok": False, "action": "PROTECTION_ERROR", "symbol": "BTCUSDT"}
        ]
        st._run_smart_guard_tick()
        self.assertFalse(st.entry_paused.is_set())

        # Критическая ошибка отсутствия SL паузит немедленно с 1-го цикла
        st.bybit.spm.audit_and_protect_open_positions.return_value = [
            {"ok": False, "action": "MISSING_STOP_LOSS", "symbol": "BTCUSDT"}
        ]
        st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._entry_pause_source, "guard_critical")
        self.assertIn("MISSING_STOP_LOSS", st.entry_pause_reason)

        # Даже после 5 чистых циклов критическая пауза НЕ снимается автоматически
        st.bybit.spm.audit_and_protect_open_positions.return_value = []
        for _ in range(5):
            st._run_smart_guard_tick()
        self.assertTrue(st.entry_paused.is_set())
        self.assertEqual(st._entry_pause_source, "guard_critical")

    def test_user_and_emergency_pauses_never_auto_recover(self) -> None:
        for src, reason in (("user", "Остановлено пользователем"), ("emergency", "Аварийный стоп")):
            st = _make_stub_state(booster_on=True, core_on=True)
            st.entry_paused.set()
            st.entry_pause_reason = reason
            st._entry_pause_source = src

            for _ in range(5):
                st._run_smart_guard_tick()

            self.assertTrue(st.entry_paused.is_set())
            self.assertEqual(st._entry_pause_source, src)
            self.assertEqual(st.entry_pause_reason, reason)

    def test_transient_pause_does_not_auto_recover_when_autopilots_disabled(self) -> None:
        st = _make_stub_state(booster_on=False, core_on=False)
        st.entry_paused.set()
        st.entry_pause_reason = "Ошибка Smart Guard: timeout"
        st._entry_pause_source = "guard_transient"

        for _ in range(5):
            st._run_smart_guard_tick()

        self.assertTrue(st.entry_paused.is_set())

    def test_ui_displays_entry_pause_reason(self) -> None:
        js_text = Path(__file__).resolve().parents[1].joinpath("dashboard_app.js").read_text(encoding="utf-8")
        self.assertIn("h.entry_pause_reason", js_text)
        self.assertIn("svcB.title = hNow.entry_pause_reason", js_text)


if __name__ == "__main__":
    unittest.main()

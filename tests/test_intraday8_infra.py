# -*- coding: utf-8 -*-
"""Инфраструктурные регрессионные тесты для релиза intraday.8."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock

from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.dashboard.live_server import ControlCenterState
from aitrade.exchange.transport import ExchangeError
from aitrade.execution.smart_position_manager import SmartPositionManager


class Intraday8InfraTests(unittest.TestCase):
    def test_isolated_guard_paths_prevent_cross_account_state_wipe(self):
        with tempfile.TemporaryDirectory() as td:
            p_core = Path(td) / "position_guard_autotrade.json"
            p_boost = Path(td) / "position_guard_booster.json"
            sp_core = SmartPositionManager("k1", "s1", guard_path=p_core)
            sp_boost = SmartPositionManager("k2", "s2", guard_path=p_boost)
            sp_core.split_tp_enabled = False
            sp_boost.split_tp_enabled = False

            sp_core.ledger = NS(active_intent=lambda *a: {"intent_id": "core-1", "strategy_id": "CORE:S3_VWAP_RANGE", "sl": 98.0})
            core_pos = {
                "symbol": "BTCUSDT", "side": "Buy", "positionIdx": 0, "size": "1",
                "avgPrice": "100", "markPrice": "100.5", "stopLoss": "98", "takeProfit": "104", "trailingStop": "0",
            }
            sp_core.paged_get = lambda path, p: [core_pos] if path.endswith("/position/list") else []
            sp_boost.paged_get = lambda path, p: []

            sp_core.audit_and_protect_open_positions()
            saved_before = json.loads(p_core.read_text(encoding="utf-8"))
            self.assertEqual(len(saved_before), 1)

            # Пустой Booster-контроллер на том же base_url не должен затирать файл Core
            sp_boost.audit_and_protect_open_positions()
            saved_after = json.loads(p_core.read_text(encoding="utf-8"))
            self.assertEqual(saved_after, saved_before)

    def test_guard_protects_positions_even_when_history_sync_fails(self):
        st = ControlCenterState.__new__(ControlCenterState)
        st.smart_guard_enabled = True
        st.split_tp_enabled = False
        st.entry_paused = threading.Event()
        st.entry_pause_reason = ""
        st.entry_paused_at = 0.0
        st.logs = []
        st.add_log = Mock()
        st.position_opened_ts = {}
        st.x_radar = Mock(enabled=False)
        st.regime_guard = Mock(time_stop_enabled=False)

        ctrl = MagicMock()
        ctrl.spm = MagicMock()
        ctrl.spm.audit_and_protect_open_positions.return_value = [{"ok": True, "symbol": "BTCUSDT", "action": "BREAKEVEN"}]
        ctrl.get_open_orders.return_value = []
        ctrl.get_positions.return_value = []
        # Ошибка Bybit 10002 на истории не должна прерывать защиту открытых позиций
        ctrl.get_closed_pnl.side_effect = ExchangeError("10002 timestamp error", code=10002)

        st._run_guard_cycle_for_controller(ctrl)
        ctrl.spm.audit_and_protect_open_positions.assert_called_once()
        self.assertFalse(st.entry_paused.is_set())

    def test_ensure_isolated_margin_does_not_call_account_mode_on_110044(self):
        spm = SmartPositionManager("k", "s")
        calls = []

        def fake_post(ep, body):
            calls.append(ep)
            raise ExchangeError("Insufficient available margin", code=110044)

        spm.signed_post = Mock(side_effect=fake_post)
        res = spm.ensure_isolated_margin("BTCUSDT", 3)
        self.assertFalse(res["ok"])
        self.assertEqual(calls, ["/v5/position/switch-isolated"])

    def test_news_radar_shield_blog_does_not_trigger_global_panic_for_link(self):
        radar = XNewsSentimentRadar()
        score, _, is_panic = radar._score_text("Inside SHIELD: Intelligent Defense for the Onchain Economy against hack and exploit")
        self.assertFalse(is_panic)
        radar.panic_active = is_panic
        mod = radar.get_coin_modifier("LINKUSDT", "LONG")
        self.assertTrue(mod["allowed"])


if __name__ == "__main__":
    unittest.main()

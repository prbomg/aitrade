# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.24-intraday.43: аудит 3 (ML карантин, booster_config guard, WAL, зеркала OI, изоляция ключей)."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.booster.ml_calibrator import AdaptiveMLCalibrator
from aitrade.booster.oi_squeeze_scanner import OISqueezeScanner
from aitrade.dashboard.live_server import BybitV5DirectController, ControlCenterState, KairosSessionAuth, create_wsgi_app
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.storage import Ledger


class TestIntraday43Audit3ResilienceFixes(unittest.TestCase):
    def test_ml_calibrator_does_not_reenter_quarantine_immediately_after_48h_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cal = AdaptiveMLCalibrator(Path(td))
            now0 = 1_700_000_000.0
            # 26 убыточных сделок -> PF < 0.75 -> QUARANTINE на 48ч
            rows = [
                {"strategy_id": "CORE:B2_DONCHIAN_INTRADAY", "closedPnl": -10.0 if i % 4 else 5.0, "updatedTime": int((now0 - 3600 + i * 10) * 1000)}
                for i in range(26)
            ]
            cal.calibrate_from_closed_pnl(rows, force=True, now_ts=now0)
            mod1 = cal.get_strategy_modifier("B2_DONCHIAN_INTRADAY")
            self.assertEqual(mod1["status"], "QUARANTINE")

            # Через 49 часов стратегия выходит в COOLING (0.50x)
            now_after = now0 + 49 * 3600
            cal.calibrate_from_closed_pnl(rows, force=True, now_ts=now_after)
            mod2 = cal.get_strategy_modifier("B2_DONCHIAN_INTRADAY")
            self.assertEqual(mod2["status"], "COOLING")
            self.assertEqual(mod2["weight"], 0.50)

            # Следующий вызов калибровки (например, +1 сделка) НЕ должен мгновенно вернуть в QUARANTINE
            rows_plus_1 = rows + [{"strategy_id": "CORE:B2_DONCHIAN_INTRADAY", "closedPnl": -5.0, "updatedTime": int((now_after + 60) * 1000)}]
            cal.calibrate_from_closed_pnl(rows_plus_1, force=True, now_ts=now_after + 120)
            mod3 = cal.get_strategy_modifier("B2_DONCHIAN_INTRADAY")
            self.assertEqual(mod3["status"], "COOLING")

            # После 5 новых сделок без улучшения (PF < 0.75) снова разрешён переход в QUARANTINE
            rows_plus_5 = rows + [
                {"strategy_id": "CORE:B2_DONCHIAN_INTRADAY", "closedPnl": -5.0, "updatedTime": int((now_after + 60 * (k + 1)) * 1000)}
                for k in range(5)
            ]
            cal.calibrate_from_closed_pnl(rows_plus_5, force=True, now_ts=now_after + 600)
            mod4 = cal.get_strategy_modifier("B2_DONCHIAN_INTRADAY")
            self.assertEqual(mod4["status"], "QUARANTINE")

    def test_cached_state_preserves_full_booster_metrics_without_truncating_to_100(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st._snapshot_lock = threading.Lock()
        st._logs_lock = threading.Lock()
        st.logs = []
        st._snapshot = {
            "closed_pnl": [{"closedPnl": 1.0}] * 100,
            "positions": [],
            "booster": {"realized_total_usd": 555.55, "trades_count": 150},
        }
        st._snapshot_ts = time.time()
        st._snapshot_error = ""
        st.scan_status = {"running": False, "last_error": "", "last_success": time.time(), "last_attempt": time.time(), "heartbeat": time.time(), "failed_cycles": 0}
        st.guard_status = {"heartbeat": time.time(), "last_success": time.time(), "last_error": ""}
        st._autopilot_thread = MagicMock()
        st._autopilot_thread.is_alive.return_value = True
        st.entry_paused = threading.Event()
        st.manual_task = {"id": "", "status": "idle", "error": ""}
        st.autopilot_booster_enabled = False
        st.autopilot_core_enabled = False
        st.booster_cycles_done = 0
        st.core_cycles_done = 0
        st.autopilot_interval_sec = 300
        st._last_auto_ts = time.time()
        st.booster = MagicMock()
        st.booster.compute_metrics.side_effect = AssertionError("cached_state must not recompute booster on truncated closed_pnl[:100]")

        out = st.cached_state()
        self.assertEqual(out["booster"]["realized_total_usd"], 555.55)
        self.assertEqual(out["booster"]["trades_count"], 150)

    def test_booster_config_does_not_clear_guard_critical_pause(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st._snapshot_lock = threading.Lock()
        st._logs_lock = threading.Lock()
        st.logs = []
        st._snapshot = {}
        st._last_booster_closed = []
        st._last_booster_positions = []
        st.entry_paused = threading.Event()
        st.entry_paused.set()
        st.entry_pause_reason = "Ошибка защиты позиции: MISSING_STOP_LOSS"
        st._entry_pause_source = "guard_critical"
        st.guard_status = {"last_error": "Ошибка защиты позиции: MISSING_STOP_LOSS"}
        st.autopilot_booster_enabled = True
        st.autopilot_core_enabled = False
        st.booster = MagicMock()
        st.booster.state = {"training_unlimited_mode": False}
        st.bybit = MagicMock()
        st.bybit.base_url = "https://api-demo.bybit.com"
        st.booster_bybit = MagicMock()
        st.settings = {}
        st.add_log = MagicMock()
        st.refresh_booster_snapshot = MagicMock()

        app = create_wsgi_app(st)
        body = json.dumps({"unlock_limits": True}).encode("utf-8")
        env = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/booster_config",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "HTTP_X_REQUESTED_WITH": "KAIROS",
            "wsgi.input": io.BytesIO(body),
        }
        status_holder = []
        raw = b"".join(app(env, lambda s, h: status_holder.append(s)))
        resp = json.loads(raw.decode("utf-8"))
        self.assertTrue(st.entry_paused.is_set())
        self.assertIn("Smart Guard", resp.get("retMsg", ""))
        st.bybit.reconcile_and_clear_unresolved.assert_called_once()
        st.booster_bybit.reconcile_and_clear_unresolved.assert_called_once()

    def test_apply_credentials_isolated_does_not_overwrite_shared_engine_client(self) -> None:
        engine = MagicMock()
        engine.client.api_key = "main-key"
        engine.client.api_secret = "main-sec"
        engine.client.trade_base_url = "https://api-demo.bybit.com"
        ctrl = BybitV5DirectController.__new__(BybitV5DirectController)
        ctrl.engine = engine
        ctrl.ledger = MagicMock()
        ctrl.spm = MagicMock()
        ctrl.apply_credentials("booster-key", "booster-sec", "https://api-demo.bybit.com", sync_engine_client=False)
        self.assertEqual(engine.client.api_key, "main-key")
        self.assertEqual(ctrl.spm.api_key, "booster-key")

    def test_sqlite_ledger_and_shadow_enable_wal_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            l_path = Path(td) / "ledger.sqlite3"
            s_path = Path(td) / "shadow.sqlite3"
            Ledger(l_path)
            LiveShadowEngine(s_path)
            for p in (l_path, s_path):
                conn = sqlite3.connect(p)
                try:
                    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    self.assertEqual(str(mode).lower(), "wal")
                finally:
                    conn.close()

    def test_oi_squeeze_scanner_falls_back_to_mirror_endpoint(self) -> None:
        sc = OISqueezeScanner()
        called = []

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"retCode":0,"result":{"list":[]}}'

        def fake_open(url, timeout=6):
            called.append(url)
            if "api.bybit.com" in url:
                raise OSError("403 geo blocked")
            return _Resp()

        with patch("aitrade.booster.oi_squeeze_scanner.urllib.request.urlopen", side_effect=fake_open):
            data = sc._curl_json("https://api.bybit.com/v5/market/tickers?category=linear")
        self.assertEqual(data["retCode"], 0)
        self.assertEqual(len(called), 2)
        self.assertIn("api-demo.bybit.com", called[1])

    def test_auth_failed_attempts_prunes_stale_ips(self) -> None:
        auth = KairosSessionAuth()
        stale_ts = time.time() - 600.0
        for i in range(505):
            auth._failed_attempts[f"10.0.{i // 255}.{i % 255}"] = [stale_ts]
        auth.record_failure({"REMOTE_ADDR": "192.168.1.10"})
        self.assertEqual(len(auth._failed_attempts), 1)
        self.assertIn("192.168.1.10", auth._failed_attempts)


if __name__ == "__main__":
    unittest.main()

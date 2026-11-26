# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aitrade.booster.env_manager import BybitEnvManager
from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard
from aitrade.dashboard.live_server import ControlCenterState
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.execution.limit_chaser import SmartLimitChaser
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.storage import Ledger


class TestIntraday54AuditHardening(unittest.TestCase):
    def test_ledger_execution_lifecycles_cache_and_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            r1 = ledger._execution_lifecycles()
            r2 = ledger._execution_lifecycles()
            self.assertIs(r1, r2)

            ledger.intent("at-1", "BTCUSDT", "CORE:T1_DONCHIAN_TSMOM", 15.0, {"side": "Buy", "qty": 0.01})
            r3 = ledger._execution_lifecycles()
            self.assertIsNot(r2, r3)
            self.assertIn("at-1", r3[0])

    def test_limit_chaser_recovers_from_corrupted_stats_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "chaser_stats.json").write_text("{corrupted json", encoding="utf-8")
            ch = SmartLimitChaser(MagicMock(), MagicMock(), tmp_p)
            self.assertEqual(ch.stats["total_orders"], 0)
            self.assertEqual(ch.stats["maker_filled"], 0)

    def test_shadow_engine_backup_to_creates_sqlite_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            sh = LiveShadowEngine(tmp_p / "shadow_lab.sqlite3")
            dest = sh.backup_to(tmp_p / "backups", keep=3)
            self.assertTrue(dest.exists())
            self.assertGreater(dest.stat().st_size, 0)

    def test_env_manager_preflight_checklist_supports_subaccounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BybitEnvManager(Path(tmp), default_demo_key="sharedKey1234", default_demo_secret="sharedSec1234")
            # У субаккаунта booster ещё нет своих ключей — чек-лист должен явно указать BOOSTER/DEMO
            chk = mgr.run_preflight_checklist("demo", account="booster")
            self.assertFalse(chk["passed"])
            self.assertEqual(chk.get("account"), "booster")
            self.assertIn("BOOSTER/DEMO", chk["summary"])

    def test_strategy_detail_does_not_truncate_at_global_50_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            ledger = Ledger(tmp_p / "demo.sqlite3")
            # 1 сделка B4 в прошлом
            ledger.intent("at-b4-1", "UNIUSDT", "BOOSTER:B4_ORB_SESSION_BREAKOUT", 15.0, {
                "side": "Buy", "qty": 10.0, "requestedQty": 10.0, "entry": 10.0, "sl": 9.5, "tp": 11.5, "orderIds": ["o-b4-1"]
            })
            ledger.finish("at-b4-1", "FILLED", {"confirmedQty": 10.0, "orderIds": ["o-b4-1"]})
            ledger.put_records("executions", [{
                "execId": "ex-b4-1", "orderId": "o-b4-1", "orderLinkId": "at-b4-1", "symbol": "UNIUSDT",
                "side": "Buy", "execType": "Trade", "execQty": "10", "closedSize": "0", "execPrice": "10.0", "execFee": "0.05", "execTime": "1700000001000"
            }], "execId")
            # 55 более свежих сделок другой стратегии (B2)
            for i in range(55):
                iid = f"at-b2-{i}"
                oid = f"o-b2-{i}"
                ledger.intent(iid, "BTCUSDT", "BOOSTER:B2_DONCHIAN_INTRADAY", 15.0, {
                    "side": "Buy", "qty": 0.01, "requestedQty": 0.01, "entry": 70000.0, "sl": 69000.0, "tp": 73000.0, "orderIds": [oid]
                })
                ledger.finish(iid, "FILLED", {"confirmedQty": 0.01, "orderIds": [oid]})
                ledger.put_records("executions", [{
                    "execId": f"ex-b2-{i}", "orderId": oid, "orderLinkId": iid, "symbol": "BTCUSDT",
                    "side": "Buy", "execType": "Trade", "execQty": "0.01", "closedSize": "0", "execPrice": "70000.0", "execFee": "0.3", "execTime": str(1700001000000 + i * 1000)
                }], "execId")

            st = ControlCenterState.__new__(ControlCenterState)
            st.settings = {"demo_lab": {"enabled": True, "admit_experimental_strategies": ["B4_ORB_SESSION_BREAKOUT"]}}
            st.paused_strategies = []
            st.bybit = MagicMock()
            st.bybit.base_url = "https://api-demo.bybit.com"
            st.bybit.ledger = ledger
            st.booster_bybit = st.bybit
            st.cached_state = lambda: {"timestamp": "2026-11-26 12:00:00", "live_strategy_metrics": {"strategies": {}, "execution_traces": []}}
            st._live_bbo_map = lambda: {}
            st.shadow_engine = LiveShadowEngine(tmp_p / "shadow.sqlite3")

            detail = st.get_strategy_detail("B4_ORB_SESSION_BREAKOUT")
            self.assertEqual(len(detail["demo_traces"]), 1)
            self.assertEqual(detail["demo_traces"][0]["intent_id"], "at-b4-1")

    def test_thread_locks_and_config_sync(self) -> None:
        rg = RegimeAndCorrelationGuard()
        self.assertTrue(hasattr(rg, "_lock"))
        ws = BybitV5WebSocketHub(["BTCUSDT"])
        m = ws.get_symbol_metrics("BTCUSDT")
        self.assertEqual(m["symbol"], "BTCUSDT")

        root = Path(__file__).resolve().parent.parent
        html = (root / "dashboard_report.html").read_text(encoding="utf-8")
        self.assertIn('id="cfg-leverage" value="3" min="1" max="3"', html)


if __name__ == "__main__":
    unittest.main()

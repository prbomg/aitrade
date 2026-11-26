import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.booster.env_manager import BybitEnvManager
from aitrade.engine import AITradeCombineEngine
from aitrade.exchange.bybit_v5 import BybitV5Client
from aitrade.storage import Ledger


class TestIntraday55Fixes(unittest.TestCase):
    def test_clear_intent_if_absent_invalidates_lifecycle_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3", environment="demo")
            ledger.intent("int-1", "BTCUSDT", "CORE:T1", 10.0, {"side": "Buy", "qty": 0.1})
            # Warm lifecycle cache
            _ = ledger._execution_lifecycles()
            ver_before = ledger._cache_version
            self.assertIsNotNone(ledger._lifecycle_cache)

            ledger.clear_intent_if_absent("int-1")
            self.assertEqual(ledger._cache_version, ver_before + 1)
            self.assertIsNone(ledger._lifecycle_cache)

    def test_bybit_v5_client_thread_safety_orderbook_and_inst_cache(self):
        client = BybitV5Client({"exchange": {"mode": "demo"}}, allow_offline_fallback=True)
        self.assertTrue(hasattr(client, "_lock"))
        self.assertIsInstance(client._inst_cache, dict)

        def fake_public_get(path, params, timeout=4.5):
            if path == "/v5/market/orderbook":
                return {"b": [["100.0", "1.0"]], "a": [["100.5", "1.0"]], "ts": 1700000000000}
            if path == "/v5/market/instruments-info":
                return {
                    "list": [
                        {
                            "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"},
                            "priceFilter": {"tickSize": "0.1"},
                        }
                    ]
                }
            return {}

        errors = []

        def worker(idx: int):
            try:
                sym = f"SYM{idx % 4}USDT"
                for _ in range(15):
                    client.get_orderbook(sym, limit=5)
                    hist = client.get_orderbook_history(sym)
                    self.assertIsInstance(hist, list)
                    self.assertLessEqual(len(hist), 40)
                    lot = client.get_instrument_lot_and_tick(sym)
                    self.assertEqual(lot, (0.01, 0.01, 0.1))
            except Exception as exc:
                errors.append(exc)

        with patch.object(client, "_public_get", side_effect=fake_public_get):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        # Mutating returned history copy must not affect internal history
        h = client.get_orderbook_history("SYM0USDT")
        orig_len = len(h)
        h.clear()
        self.assertEqual(len(client.get_orderbook_history("SYM0USDT")), orig_len)

    def test_env_manager_preflight_checklist_returns_account_and_scope_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = BybitEnvManager(Path(tmp), default_demo_key="shared_k", default_demo_secret="shared_s")
            # Missing keys path for booster/demo
            res_missing = mgr.run_preflight_checklist("demo", account="booster")
            self.assertEqual(res_missing["account"], "booster")
            self.assertFalse(res_missing["passed"])
            self.assertIn("BOOSTER/DEMO", res_missing["summary"])

            # Save subaccount keys and mock successful Bybit responses
            mgr.save_subaccount_keys("booster", "demo", "sub_k_123456", "sub_s_123456")

            def fake_signed_get(base_url, api_key, api_secret, endpoint, params):
                if endpoint == "/v5/user/query-api":
                    return {
                        "retCode": 0,
                        "result": {
                            "userID": "999",
                            "readOnly": 0,
                            "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []},
                        },
                    }
                if endpoint == "/v5/account/wallet-balance":
                    return {"retCode": 0, "result": {"list": [{"totalEquity": "250.50"}]}}
                return {"retCode": -1, "retMsg": "unexpected"}

            with patch.object(mgr, "_signed_get", side_effect=fake_signed_get):
                res_ok = mgr.run_preflight_checklist("demo", account="booster")

            self.assertTrue(res_ok["passed"])
            self.assertEqual(res_ok["account"], "booster")
            self.assertIn("BOOSTER/DEMO", res_ok["summary"])
            self.assertEqual(mgr.state["last_checklist"]["account"], "booster")

    def test_scan_market_skips_private_account_rest_calls_when_disabled(self):
        settings = {
            "offline_simulation": True,
            "exchange": {
                "mode": "demo",
                "demo_api_key": "k_test",
                "demo_api_secret": "s_test",
            },
            "universe": {"core_pairs": ["BTCUSDT"], "autotrade_pairs": ["BTCUSDT"], "booster_pairs": ["BTCUSDT"]},
            "risk": {"allocated_equity_usdt": 1000.0, "max_concurrent_positions": 3},
            "demo_lab": {"enabled": True},
        }
        eng = AITradeCombineEngine(settings)
        eng.demo_executor.get_account_equity_usdt = MagicMock(return_value=1000.0)
        eng.client.get_open_orders = MagicMock(return_value=[{"orderId": "o1"}])
        eng.client.get_open_positions = MagicMock(return_value=[{"symbol": "BTCUSDT", "size": "1"}])

        res_skip = eng.scan_market(symbols=["BTCUSDT"], auto_execute_demo=False, fetch_live_account_state=False)
        eng.client.get_open_orders.assert_not_called()
        eng.client.get_open_positions.assert_not_called()
        self.assertEqual(res_skip["live_open_orders"], [])
        self.assertEqual(res_skip["live_open_positions"], [])

        res_fetch = eng.scan_market(symbols=["BTCUSDT"], auto_execute_demo=False, fetch_live_account_state=True)
        eng.client.get_open_orders.assert_called_once()
        eng.client.get_open_positions.assert_called_once()
        self.assertEqual(len(res_fetch["live_open_orders"]), 1)
        self.assertEqual(len(res_fetch["live_open_positions"]), 1)

    def test_dashboard_js_displays_subaccount_scope_in_checklist_header(self):
        js_text = Path("/Users/sergejrubcov/AItrade/dashboard_app.js").read_text(encoding="utf-8")
        self.assertIn('chk.account && chk.account !== "shared"', js_text)
        self.assertIn('document.getElementById("env-chk-time").textContent = "Проверка: " + (chk.checked_at || "—") + " [" + chkScope + "]";', js_text)


if __name__ == "__main__":
    unittest.main()

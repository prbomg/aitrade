import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard
from aitrade.config import BASE_DIR
from aitrade.engine import AITradeCombineEngine
from aitrade.execution.shadow_engine import LiveShadowEngine


class TestIntraday59AuditFixes(unittest.TestCase):
    def test_scan_market_skips_wallet_equity_rest_call_when_fetch_live_disabled(self):
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
        eng.demo_executor.get_account_equity_usdt = MagicMock(side_effect=RuntimeError("must not call private wallet API"))
        eng.client.get_open_orders = MagicMock(return_value=[])
        eng.client.get_open_positions = MagicMock(return_value=[])

        res = eng.scan_market(symbols=["BTCUSDT"], auto_execute_demo=False, fetch_live_account_state=False)
        eng.demo_executor.get_account_equity_usdt.assert_not_called()
        self.assertEqual(res["account_equity_usdt"], 1000.0)

    def test_shadow_engine_closed_cache_detects_closes_from_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shadow.sqlite3"
            sh_a = LiveShadowEngine(db_path)
            sh_b = LiveShadowEngine(db_path)

            # Warm closed cache on sh_a while empty
            sum_empty = sh_a.get_summary()
            self.assertEqual(sum_empty["total_closed"], 0)

            # Open and close a trade via sh_b
            opened = sh_b.record_signals(
                [
                    {
                        "signal_id": "sig-sh-cross-1",
                        "signal_created_ms": 1700000000000,
                        "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "entry_price": 100.0,
                        "stop_loss": 98.0,
                        "take_profit": 104.0,
                    }
                ],
                {"BTCUSDT": (99.9, 100.0)},
                now_ts=1700000000.0,
            )
            self.assertEqual(opened, 1)
            closed = sh_b.update_open_positions({"BTCUSDT": (105.0, 105.1)}, now_ts=1700000600.0)
            self.assertEqual(closed, 1)

            # sh_a must immediately observe the closed trade via DB revision
            sum_after = sh_a.get_summary()
            self.assertEqual(sum_after["total_closed"], 1)
            self.assertEqual(sum_after["recent_closed"][0]["signal_id"], "sig-sh-cross-1")

    def test_regime_guard_atomic_last_regime_update_under_concurrency(self):
        guard = RegimeAndCorrelationGuard()
        fake_kline = {
            "retCode": 0,
            "result": {
                "list": [
                    ["1700000300000", "100.0", "101.0", "99.5", "100.5", "10", "1000"],
                    ["1700000000000", "99.5", "100.5", "99.0", "100.0", "10", "1000"],
                    ["1699999700000", "99.0", "100.0", "98.5", "99.5", "10", "1000"],
                ]
            },
        }
        errors = []

        def worker():
            try:
                for _ in range(30):
                    with guard._lock:
                        guard._last_fetch_ts = 0.0
                    res = guard.update_btc_regime({"metrics": [{"symbol": "BTCUSDT", "mid_price": 100.5, "obi_top5_pct": 6.0, "cvd_usd": 5000.0}]})
                    self.assertEqual(res["btc_price"], 100.5)
                    self.assertIn("state", res)
            except Exception as exc:
                errors.append(exc)

        with patch.object(guard, "_curl_json", return_value=fake_kline):
            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])

    def test_all_test_files_are_listed_in_release_manifest(self):
        manifest = json.loads((BASE_DIR / "release-manifest.json").read_text(encoding="utf-8"))
        listed = set((manifest.get("files") or {}).keys())
        actual_tests = {f"tests/{p.name}" for p in (BASE_DIR / "tests").glob("test_*.py")}
        missing = sorted(actual_tests - listed)
        self.assertEqual(missing, [], f"Test files missing from release-manifest.json: {missing}")


if __name__ == "__main__":
    unittest.main()

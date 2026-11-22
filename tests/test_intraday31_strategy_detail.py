import json
import tempfile
import unittest
from pathlib import Path

from aitrade.dashboard.live_server import ControlCenterState, create_wsgi_app
from aitrade.execution.shadow_engine import LiveShadowEngine


class TestIntraday31StrategyDetail(unittest.TestCase):
    def test_shadow_engine_get_strategy_detail_returns_curve_and_trades(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng = LiveShadowEngine(Path(td) / "shadow.sqlite3")
            t0 = 1_800_000_000.0
            eng.record_signals(
                [
                    {"strategy_id": "B2_DONCHIAN_INTRADAY", "symbol": "SOLUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 106.0},
                    {"strategy_id": "B2_DONCHIAN_INTRADAY", "symbol": "ETHUSDT", "side": "Buy", "entry_price": 3000.0, "stop_loss": 2940.0, "take_profit_2": 3180.0},
                ],
                {"SOLUSDT": (99.99, 100.01), "ETHUSDT": (2999.5, 3000.5)},
                now_ts=t0,
            )
            # Закрываем SOLUSDT по TP, оставляем ETHUSDT открытым
            eng.update_open_positions({"SOLUSDT": (106.2, 106.3), "ETHUSDT": (3030.0, 3031.0)}, now_ts=t0 + 3600)
            det = eng.get_strategy_detail("B2_DONCHIAN_INTRADAY", {"ETHUSDT": (3030.0, 3031.0)})
            self.assertEqual(det["closed_trades"], 1)
            self.assertEqual(det["open_trades"], 1)
            self.assertEqual(len(det["closed_trades_list"]), 1)
            self.assertEqual(det["closed_trades_list"][0]["exit_reason"], "TP")
            self.assertEqual(len(det["open_positions"]), 1)
            self.assertGreaterEqual(len(det["equity_curve"]), 2)
            self.assertGreater(det["pod_equity_usd"], 1035.0)

    def test_api_strategy_detail_endpoint_returns_full_profile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st = ControlCenterState.__new__(ControlCenterState)
            st.settings = {
                "demo_lab": {"enabled": True, "admit_experimental_strategies": ["B2_DONCHIAN_INTRADAY"]},
                "universe": {"core_pairs": ["BTCUSDT"]},
            }
            st.bybit = type("B", (), {"base_url": "https://api-demo.bybit.com"})()
            st.ws_hub = type("W", (), {"symbols": ["BTCUSDT"], "get_bbo": lambda self, s, max_age=30: (95000.0, 95001.0)})()
            st.shadow_engine = LiveShadowEngine(Path(td) / "shadow.sqlite3")
            st.cached_state = lambda: {  # type: ignore
                "timestamp": "2026-11-22 12:00:00",
                "wfa_validation": {"strategies": {"T1_DONCHIAN_TSMOM": {"wfe_passed": True, "oos_trades": 92, "oos_pf": 2.16}}},
                "strategy_funnel_24h": {"strategies": {"T1_DONCHIAN_TSMOM": {"candidates": 2, "submitted": 1, "last_idle_reason": "OK"}}},
                "live_strategy_metrics": {"strategies": {"T1_DONCHIAN_TSMOM": {"trades": 1, "net_pnl_usd": 25.0}}, "execution_traces": []},
                "positions": [],
                "ml_calibration": {"strategy_list": []},
            }
            app = create_wsgi_app(st)
            captured = {}

            def start_resp(status, headers):
                captured["status"] = status

            body = b"".join(app({"REQUEST_METHOD": "GET", "PATH_INFO": "/api/strategy_detail", "QUERY_STRING": "id=T1_DONCHIAN_TSMOM"}, start_resp))
            self.assertEqual(captured["status"], "200 OK")
            data = json.loads(body.decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["strategy_id"], "T1_DONCHIAN_TSMOM")
            self.assertEqual(data["meta"]["admission_status"], "WFA_PASSED")
            self.assertEqual(data["wfa"]["oos_pf"], 2.16)
            self.assertEqual(data["demo_metrics"]["net_pnl_usd"], 25.0)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard
from aitrade.dashboard.live_server import ControlCenterState, create_wsgi_app
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.storage import Ledger


class TestIntraday51AuditHardening(unittest.TestCase):
    def test_ledger_indexes_and_prune_old_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.sqlite3")
            with ledger.connect() as c:
                idx_names = {r[1] for r in c.execute("PRAGMA index_list(events)").fetchall()}
                self.assertIn("idx_events_kind_ts", idx_names)
                old_ts = time.time() - 40 * 86400.0
                c.execute("INSERT INTO events(ts, kind, payload) VALUES (?, 'LOG', '{}')", (old_ts,))
                c.execute("INSERT INTO events(ts, kind, payload) VALUES (?, 'SIGNAL_DECISION', '{}')", (old_ts,))
                c.execute("INSERT INTO events(ts, kind, payload) VALUES (?, 'EXIT_SUBMITTED', '{}')", (old_ts,))
            ledger.event("LOG", {"message": "fresh"})
            deleted = ledger.prune_old_events(max_log_age_days=14, max_decision_age_days=30)
            self.assertEqual(deleted, 2)
            with ledger.connect() as c:
                rem = c.execute("SELECT kind FROM events").fetchall()
                kinds = sorted(r[0] for r in rem)
                self.assertEqual(kinds, ["EXIT_SUBMITTED", "LOG"])

    def test_shadow_engine_closed_cache_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = LiveShadowEngine(Path(tmp) / "shadow.sqlite3")
            t0 = 1_800_000_000.0
            eng.record_signals(
                [
                    {
                        "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "entry_price": 100.0,
                        "stop_loss": 98.0,
                        "take_profit_2": 104.0,
                        "signal_id": "sig-1",
                    }
                ],
                {"BTCUSDT": (99.99, 100.01)},
                now_ts=t0,
            )
            s1 = eng.get_summary({"BTCUSDT": (101.0, 101.02)})
            self.assertEqual(s1["total_open"], 1)
            self.assertEqual(s1["total_closed"], 0)
            self.assertGreater(s1["leaderboard"][0]["unrealized_pnl_usd"], 0.0)

            closed = eng.update_open_positions({"BTCUSDT": (104.5, 104.6)}, now_ts=t0 + 120)
            self.assertEqual(closed, 1)
            s2 = eng.get_summary({"BTCUSDT": (104.5, 104.6)})
            self.assertEqual(s2["total_open"], 0)
            self.assertEqual(s2["total_closed"], 1)
            self.assertGreater(s2["leaderboard"][0]["net_pnl_usd"], 0.0)

            with eng._connect() as conn:
                conn.execute(
                    "INSERT INTO shadow_signal_decisions (ts, signal_id, strategy_id, symbol, side, decision, reason, shadow_trade_id) "
                    "VALUES (?, 'old-sig', 'B4_ORB_SESSION_BREAKOUT', 'BTCUSDT', 'Buy', 'SKIPPED', 'old', '')",
                    (time.time() - 45 * 86400.0,),
                )
            pruned = eng.prune_old_decisions(max_age_days=30)
            self.assertEqual(pruned, 1)

    def test_regime_guard_multidomain_fallback(self):
        guard = RegimeAndCorrelationGuard()
        tried = []

        def fake_fetch(url: str):
            tried.append(url)
            if "api-demo.bybit.com" in url:
                raise RuntimeError("502")
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        ["1", "100000", "100500", "99900", "100100"],
                        ["2", "99900", "100100", "99800", "100000"],
                        ["3", "99800", "100000", "99700", "99900"],
                    ]
                },
            }

        guard._curl_json = fake_fetch  # type: ignore[method-assign]
        reg = guard.update_btc_regime({"metrics": []})
        self.assertGreaterEqual(len(tried), 2)
        self.assertEqual(reg["btc_price"], 100100.0)

    def test_wsgi_csp_header_and_no_contradictory_cors(self):
        st = ControlCenterState.__new__(ControlCenterState)
        st.release_manifest = {"version": "2026.11.26-intraday.51"}
        st.logs = []
        st.add_log = lambda msg: st.logs.insert(0, msg)
        app = create_wsgi_app(st)
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        app({"REQUEST_METHOD": "GET", "PATH_INFO": "/api/auth/status"}, start_response)
        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Content-Security-Policy", captured["headers"])
        self.assertNotIn("Access-Control-Allow-Methods", captured["headers"])


if __name__ == "__main__":
    unittest.main()

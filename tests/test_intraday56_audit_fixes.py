import tempfile
import threading
import unittest
from pathlib import Path

from aitrade.config import BASE_DIR
from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.execution.audit_metrics import build_shadow_demo_reconciliation
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.storage import Ledger


class TestIntraday56AuditFixes(unittest.TestCase):
    def test_shadow_closed_trades_sorted_by_closed_ts_for_equity_and_drawdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            eng = LiveShadowEngine(Path(tmp) / "shadow.sqlite3")
            # Open Trade A at t=100 (long BTC, +100 profit) and Trade B at t=200 (long ETH, -50 loss)
            # Close Trade B FIRST at t=300 (-50), then close Trade A LATER at t=400 (+100).
            # In realization order (closed_ts ASC): equity goes 1000 -> 950 -> 1050 (max_dd = 50).
            # In entry order (opened_ts ASC): equity would go 1000 -> 1100 -> 1050 (wrong curve & recent order).
            with eng._lock, eng._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO shadow_trades
                    (id, strategy_id, symbol, side, status, opened_ts, closed_ts, max_hold_min,
                     signal_entry, entry_px, sl_px, tp_px, exit_px, qty, notional_usd, risk_usd,
                     fees_usd, pnl_usd, realized_r, exit_reason, signal_id, signal_created_ms)
                    VALUES
                    ('tA', 'B4_ORB_SESSION_BREAKOUT', 'BTCUSDT', 'Buy', 'CLOSED', 100.0, 400.0, 480,
                     100.0, 100.0, 95.0, 110.0, 110.0, 10.0, 1000.0, 50.0, 1.0, 100.0, 2.0, 'TP', 'sig-A', 100000),
                    ('tB', 'B4_ORB_SESSION_BREAKOUT', 'ETHUSDT', 'Buy', 'CLOSED', 200.0, 300.0, 480,
                     100.0, 100.0, 95.0, 110.0, 95.0, 10.0, 1000.0, 50.0, 1.0, -50.0, -1.0, 'SL', 'sig-B', 200000)
                    """
                )
                eng._closed_ver += 1

            summary = eng.get_summary()
            self.assertEqual(summary["recent_closed"][0]["id"], "tA")
            self.assertEqual(summary["recent_closed"][1]["id"], "tB")

            detail = eng.get_strategy_detail("B4_ORB_SESSION_BREAKOUT")
            self.assertEqual(detail["equity_curve"], [1000.0, 950.0, 1050.0])
            self.assertEqual(detail["max_dd_usd"], 50.0)
            self.assertEqual(detail["closed_trades_list"][0]["id"], "tA")
            self.assertEqual(detail["closed_trades_list"][1]["id"], "tB")

    def test_reconciliation_includes_archived_strategies_demo_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            sh = LiveShadowEngine(Path(tmp) / "shadow.sqlite3")
            ld = Ledger(Path(tmp) / "ledger.sqlite3", environment="demo")
            sig_id = "sig-archived-s3-1"
            created_ms = 1700000000000

            sh.record_signals(
                [
                    {
                        "signal_id": sig_id,
                        "signal_created_ms": created_ms,
                        "strategy_id": "S3_VWAP_RANGE",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "entry_price": 100.0,
                        "stop_loss": 98.0,
                        "take_profit": 104.0,
                    }
                ],
                {"BTCUSDT": (99.9, 100.0)},
                now_ts=created_ms / 1000.0,
            )

            ld.intent(
                "at-s3-1",
                "BTCUSDT",
                "CORE:S3_VWAP_RANGE",
                15.0,
                {
                    "side": "Buy",
                    "qty": 1.0,
                    "requestedQty": 1.0,
                    "signal_id": sig_id,
                    "signal_created_ms": created_ms,
                    "entry": 100.0,
                    "sl": 98.0,
                    "tp": 104.0,
                    "orderId": "ord-s3-1",
                },
            )
            ld.put_records(
                "executions",
                [
                    {
                        "execId": "ex-s3-1",
                        "orderId": "ord-s3-1",
                        "orderLinkId": "at-s3-1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execQty": "1.0",
                        "closedSize": "0",
                        "execPrice": "100.0",
                        "execFee": "0.02",
                        "execTime": str(created_ms + 500),
                        "execType": "Trade",
                    }
                ],
                "execId",
            )

            rec_filtered = build_shadow_demo_reconciliation(
                sh, [("autotrade", ld)], strategy_id="S3_VWAP_RANGE", limit=50
            )
            self.assertTrue(rec_filtered["ok"])
            self.assertEqual(rec_filtered["summary"]["matched_both"], 1)
            self.assertEqual(rec_filtered["signals"][0]["pair_status"], "MATCHED_BOTH")

            rec_all = build_shadow_demo_reconciliation(sh, [("autotrade", ld)], strategy_id=None, limit=50)
            self.assertTrue(rec_all["ok"])
            self.assertEqual(rec_all["summary"]["matched_both"], 1)

    def test_x_news_radar_thread_safe_reads_under_concurrent_updates(self):
        radar = XNewsSentimentRadar()
        errors = []

        def writer():
            try:
                for i in range(100):
                    with radar._lock:
                        radar.panic_active = (i % 2 == 0)
                        radar.panic_reason = f"panic-{i}" if radar.panic_active else ""
                        radar.global_score = -25.0 if (i % 2 == 0) else 25.0
                        radar.coin_sentiment = {
                            "BTCUSDT": {
                                "score": -45 if (i % 2 == 0) else 45,
                                "count": 1,
                                "last_headline": f"headline-{i}",
                                "triggers": "test",
                            }
                        }
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(100):
                    m = radar.get_coin_modifier("BTCUSDT", "LONG")
                    e = radar.evaluate_open_position("BTCUSDT", "Buy")
                    self.assertIn("allowed", m)
                    self.assertIn("action", e)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_ws_stream_reconnect_state_is_lock_guarded(self):
        src = (BASE_DIR / "aitrade" / "exchange" / "ws_stream.py").read_text(encoding="utf-8")
        self.assertIn("with self._lock:\n                    self.book_timestamps.clear()", src)
        self.assertIn("with self._lock:\n                    self.connected = False", src)
        hub = BybitV5WebSocketHub(["BTCUSDT"])
        self.assertTrue(hasattr(hub, "_lock"))


if __name__ == "__main__":
    unittest.main()

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from aitrade.config import BASE_DIR
from aitrade.dashboard.live_server import BybitV5DirectController, ControlCenterState
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.storage import Ledger


class TestIntraday57AuditFixes(unittest.TestCase):
    def _make_guard_state(self, ctrl1, ctrl2=None):
        st = ControlCenterState.__new__(ControlCenterState)
        st.started_at = time.time() - 120.0
        st.smart_guard_enabled = True
        st.split_tp_enabled = False
        st.bybit = ctrl1
        st.booster_bybit = ctrl2 if ctrl2 is not None else ctrl1
        st.ws_hub = SimpleNamespace(get_summary=lambda: {"connected": False, "last_msg_age_sec": 60.0})
        st.guard_status = {"heartbeat": time.time(), "last_success": 0.0, "last_error": ""}
        st.entry_paused = threading.Event()
        st.entry_pause_reason = ""
        st.entry_paused_at = 0.0
        st._entry_pause_source = ""
        st._guard_consecutive_errors = 0
        st._guard_consecutive_ok = 0
        st.position_opened_ts = {}
        st._state_save_lock = threading.RLock()
        st.logs = []
        st._logs_lock = threading.Lock()
        st.add_log = lambda msg: st.logs.append(str(msg))
        return st

    def _make_stub_ctrl(self, open_orders_fn, cancel_fn):
        ctrl = MagicMock()
        ctrl.get_open_orders.side_effect = open_orders_fn
        ctrl.cancel_order.side_effect = cancel_fn
        ctrl.get_positions.return_value = []
        ctrl.get_closed_pnl.return_value = []
        ctrl.spm = MagicMock()
        ctrl.spm.audit_and_protect_open_positions.return_value = []
        return ctrl

    def test_dead_mans_switch_get_open_orders_failure_still_protects_all_accounts(self):
        # Controller 1 fails on get_open_orders in Dead-Man Switch, Controller 2 succeeds
        ctrl1_calls = {"ord": 0}

        def ctrl1_orders():
            ctrl1_calls["ord"] += 1
            if ctrl1_calls["ord"] == 1:
                raise RuntimeError("Bybit timeout on ctrl1")
            return []

        ctrl1 = self._make_stub_ctrl(ctrl1_orders, lambda s, oid: {"ok": True})
        ctrl2 = self._make_stub_ctrl(
            lambda: [{"symbol": "ETHUSDT", "orderId": "o-eth-1", "reduceOnly": False}],
            lambda s, oid: {"ok": True},
        )
        st = self._make_guard_state(ctrl1, ctrl2)
        st._run_smart_guard_tick()

        # Controller 2 entry order was still cancelled despite ctrl1 failure
        ctrl2.cancel_order.assert_called_once_with("ETHUSDT", "o-eth-1")
        # Both controllers STILL ran position protection!
        ctrl1.spm.audit_and_protect_open_positions.assert_called_once()
        ctrl2.spm.audit_and_protect_open_positions.assert_called_once()
        self.assertIn("Dead-Man Switch", st.guard_status["last_error"])

    def test_dead_mans_switch_cancel_exception_still_cancels_next_order_and_protects_positions(self):
        cancelled_ids = []

        def cancel_fn(sym, oid):
            if oid == "ord-bad":
                raise RuntimeError("cancel failed")
            cancelled_ids.append(oid)
            return {"ok": True}

        ctrl = self._make_stub_ctrl(
            lambda: [
                {"symbol": "BTCUSDT", "orderId": "ord-bad", "reduceOnly": False},
                {"symbol": "SOLUSDT", "orderId": "ord-ok", "reduceOnly": False},
                {"symbol": "BTCUSDT", "orderId": "ord-sl", "reduceOnly": True, "stopOrderType": "StopLoss"},
            ],
            cancel_fn,
        )
        st = self._make_guard_state(ctrl)
        st._run_smart_guard_tick()

        # Second entry order cancelled; protective SL skipped; position protection ran
        self.assertEqual(cancelled_ids, ["ord-ok"])
        ctrl.spm.audit_and_protect_open_positions.assert_called_once()
        self.assertIn("Dead-Man Switch", st.guard_status["last_error"])

    def test_no_hardcoded_mac_user_paths_in_test_suite(self):
        tests_dir = BASE_DIR / "tests"
        offenders = []
        for p in sorted(tests_dir.glob("test_*.py")):
            if p.name == Path(__file__).name:
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")
            if "/Users/" in txt:
                offenders.append(p.name)
        self.assertEqual(offenders, [], f"Hardcoded /Users/ paths found in: {offenders}")

    def test_ledger_lifecycle_cache_detects_writes_from_second_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shared_ledger.sqlite3"
            ledger_a = Ledger(db_path, environment="demo")
            ledger_b = Ledger(db_path, environment="demo")

            # Warm cache on ledger_a while empty
            intents_a, lots_a, _ = ledger_a._execution_lifecycles()
            self.assertEqual(intents_a, {})
            self.assertEqual(lots_a, {})
            self.assertIsNone(ledger_a.active_intent("BTCUSDT", "Buy"))

            # Write intent and execution via second Ledger instance
            ledger_b.intent(
                "at-cross-1",
                "BTCUSDT",
                "BOOSTER:B4_ORB_SESSION_BREAKOUT",
                15.0,
                {"side": "Buy", "qty": 0.1, "orderId": "ord-cross-1"},
            )
            ledger_b.put_records(
                "executions",
                [
                    {
                        "execId": "ex-cross-1",
                        "orderId": "ord-cross-1",
                        "orderLinkId": "at-cross-1",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "execQty": "0.1",
                        "closedSize": "0",
                        "execTime": "1700000001000",
                        "execType": "Trade",
                    }
                ],
                "execId",
            )

            # First instance must detect DB revision change and reload immediately
            active = ledger_a.active_intent("BTCUSDT", "Buy", size=0.1)
            self.assertIsNotNone(active)
            self.assertEqual(active["intent_id"], "at-cross-1")
            self.assertEqual(active["strategy_id"], "BOOSTER:B4_ORB_SESSION_BREAKOUT")

    def test_ws_warmup_enforces_connection_epoch_and_per_symbol_data(self):
        hub = BybitV5WebSocketHub(["BTCUSDT", "SOLUSDT"])
        now = time.time()
        # Simulate long-running process whose WS just reconnected 15 seconds ago
        hub.connected = True
        hub.stream_started_at = now - 15.0
        hub.book_timestamps["BTCUSDT"] = now
        hub.orderbooks["BTCUSDT"] = {"bids": {100.0: 5.0}, "asks": {100.1: 5.0}}
        hub.trade_events["BTCUSDT"].append((now - 1.0, 500.0, True))
        owner = SimpleNamespace(started_at=now - 3600.0, ws_hub=hub)

        # Flow-dependent Booster must be blocked after reconnect even though process is 1h old
        with self.assertRaises(ValueError):
            BybitV5DirectController._verify_booster_ws_warmup(
                owner, "BTCUSDT", "BOOSTER:OI_SQUEEZE", bbo_from_ws=True, demo_lab_on=True
            )

        # Candle-driven Booster passes with fresh BBO even during flow warmup
        BybitV5DirectController._verify_booster_ws_warmup(
            owner, "BTCUSDT", "BOOSTER:B4_ORB_SESSION_BREAKOUT", bbo_from_ws=True, demo_lab_on=False
        )
        # Candle-driven Booster without WS BBO outside Demo Lab is rejected
        with self.assertRaises(ValueError):
            BybitV5DirectController._verify_booster_ws_warmup(
                owner, "BTCUSDT", "BOOSTER:B4_ORB_SESSION_BREAKOUT", bbo_from_ws=False, demo_lab_on=False
            )

        # Now advance WS connection epoch past 120s: BTCUSDT has book + trades, SOLUSDT does not
        hub.stream_started_at = now - 150.0
        BybitV5DirectController._verify_booster_ws_warmup(
            owner, "BTCUSDT", "BOOSTER:OI_SQUEEZE", bbo_from_ws=True, demo_lab_on=False
        )
        with self.assertRaises(ValueError):
            BybitV5DirectController._verify_booster_ws_warmup(
                owner, "SOLUSDT", "BOOSTER:OI_SQUEEZE", bbo_from_ws=True, demo_lab_on=False
            )


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from aitrade.dashboard.live_server import (
    ControlCenterState,
    compute_portfolio_risk_details,
)
from aitrade.execution.audit_metrics import build_shadow_demo_reconciliation
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.storage import Ledger


class _BrokenShadowEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def _connect(self):
        raise RuntimeError("sqlite disk I/O error")


class _MockEmergencyCtrl:
    """Сценарий: первый get_positions после закрытия пуст, но при финальной отмене ордера появляется новая позиция."""

    def __init__(self, new_pos_has_sl: bool = True) -> None:
        self._pos_calls = 0
        self._new_pos_has_sl = new_pos_has_sl
        self._orders = [
            {"symbol": "BTCUSDT", "orderId": "ord-entry-1", "reduceOnly": False, "stopOrderType": ""},
        ]

    def get_open_orders(self):
        return list(self._orders)

    def cancel_order(self, symbol: str, order_id: str):
        self._orders = [o for o in self._orders if o.get("orderId") != order_id]
        return {"ok": True}

    def get_positions(self):
        self._pos_calls += 1
        if self._pos_calls == 1:
            return [{"symbol": "ETHUSDT", "side": "Buy", "size": "1", "positionIdx": 0, "stopLoss": "2900"}]
        if self._pos_calls == 2:
            # Сразу после close_position снимок пуст, но есть остаточный ордер
            self._orders = [{"symbol": "SOLUSDT", "orderId": "ord-late-2", "reduceOnly": False, "stopOrderType": ""}]
            return []
        # После отмены ордера появилась исполнившаяся позиция
        return [
            {
                "symbol": "SOLUSDT",
                "side": "Buy",
                "size": "5",
                "positionIdx": 0,
                "stopLoss": "120" if self._new_pos_has_sl else "0",
            }
        ]

    def close_position(self, symbol, side, size, position_idx=0, lock_timeout=None, exit_reason=""):
        return {"ok": True, "orderId": "close-1"}


class TestIntraday53ReconciliationRiskAndUI(unittest.TestCase):
    def test_reconciliation_limit_invariance_and_beyond_50_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            shadow = LiveShadowEngine(tmp_p / "shadow.sqlite3")
            ledger = Ledger(tmp_p / "demo.sqlite3")
            base_ms = int(time.time() * 1000) - 3_600_000

            # 1. Записываем 1 сигнал B4, который был и в Shadow, и в Demo (и окажется глубже 50 свежих трасс)
            b4_matched_sig = "sig-b4-old-matched"
            shadow.record_signals(
                [
                    {
                        "signal_id": b4_matched_sig,
                        "signal_created_ms": base_ms,
                        "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                        "symbol": "UNIUSDT",
                        "side": "Buy",
                        "entry_price": 10.0,
                        "stop_loss": 9.5,
                        "take_profit": 11.5,
                    }
                ],
                {"UNIUSDT": (9.99, 10.01)},
            )
            ledger.intent(
                "at-b4-old-1",
                "UNIUSDT",
                "BOOSTER:B4_ORB_SESSION_BREAKOUT",
                15.0,
                {
                    "side": "Buy",
                    "qty": 30.0,
                    "requestedQty": 30.0,
                    "signal_id": b4_matched_sig,
                    "signal_created_ms": base_ms,
                    "entry": 10.0,
                    "sl": 9.5,
                    "tp": 11.5,
                    "orderIds": ["ord-b4-old-1"],
                },
            )
            ledger.finish("at-b4-old-1", "FILLED", {"confirmedQty": 30.0, "orderIds": ["ord-b4-old-1"]})
            ledger.put_records(
                "executions",
                [
                    {
                        "execId": "ex-b4-old-1",
                        "orderId": "ord-b4-old-1",
                        "orderLinkId": "at-b4-old-1",
                        "symbol": "UNIUSDT",
                        "side": "Buy",
                        "execType": "Trade",
                        "execQty": "30",
                        "closedSize": "0",
                        "execPrice": "10.01",
                        "execFee": "0.15",
                        "execTime": str(base_ms + 500),
                    }
                ],
                "execId",
            )
            ledger.event(
                "SIGNAL_DECISION",
                {
                    "signal_id": b4_matched_sig,
                    "signal_created_ms": base_ms,
                    "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                    "symbol": "UNIUSDT",
                    "side": "Buy",
                    "decision": "SUBMITTED",
                    "reason": "",
                    "intent_id": "at-b4-old-1",
                },
            )

            # 2. Записываем 3 сигнала B4, пропущенных в Demo с явной причиной SKIPPED
            for idx in range(3):
                sid = f"sig-b4-skip-{idx}"
                sym = f"COIN{idx}USDT"
                shadow.record_signals(
                    [
                        {
                            "signal_id": sid,
                            "signal_created_ms": base_ms + (idx + 1) * 1000,
                            "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                            "symbol": sym,
                            "side": "Buy",
                            "entry_price": 100.0,
                            "stop_loss": 98.0,
                            "take_profit": 106.0,
                        }
                    ],
                    {sym: (99.9, 100.1)},
                )
                ledger.event(
                    "SIGNAL_DECISION",
                    {
                        "signal_id": sid,
                        "signal_created_ms": base_ms + (idx + 1) * 1000,
                        "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                        "symbol": sym,
                        "side": "Buy",
                        "decision": "SKIPPED",
                        "reason": "Лимит слотов портфеля",
                        "intent_id": "",
                    },
                )

            # 3. Забиваем журнал 60 более свежими событиями и трассами ДРУГОЙ стратегии (B2)
            for j in range(60):
                other_sig = f"sig-b2-noise-{j}"
                ts_j = base_ms + 100_000 + j * 1000
                iid = f"at-b2-{j}"
                ledger.intent(
                    iid,
                    "BTCUSDT",
                    "BOOSTER:B2_DONCHIAN_INTRADAY",
                    15.0,
                    {
                        "side": "Buy",
                        "qty": 0.01,
                        "requestedQty": 0.01,
                        "signal_id": other_sig,
                        "signal_created_ms": ts_j,
                        "entry": 70000.0,
                        "sl": 69000.0,
                        "tp": 73000.0,
                        "orderIds": [f"ord-b2-{j}"],
                    },
                )
                ledger.finish(iid, "FILLED", {"confirmedQty": 0.01, "orderIds": [f"ord-b2-{j}"]})
                ledger.put_records(
                    "executions",
                    [
                        {
                            "execId": f"ex-b2-{j}",
                            "orderId": f"ord-b2-{j}",
                            "orderLinkId": iid,
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "execType": "Trade",
                            "execQty": "0.01",
                            "closedSize": "0",
                            "execPrice": "70005.0",
                            "execFee": "0.35",
                            "execTime": str(ts_j + 200),
                        }
                    ],
                    "execId",
                )
                ledger.event(
                    "SIGNAL_DECISION",
                    {
                        "signal_id": other_sig,
                        "signal_created_ms": ts_j,
                        "strategy_id": "B2_DONCHIAN_INTRADAY",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "decision": "SUBMITTED",
                        "reason": "",
                        "intent_id": iid,
                    },
                )

            # 4. Проверяем инвариантность итогов и причин при limit=2, 25, 200
            r2 = build_shadow_demo_reconciliation(shadow, [("autotrade", ledger)], strategy_id="B4_ORB_SESSION_BREAKOUT", limit=2)
            r25 = build_shadow_demo_reconciliation(shadow, [("autotrade", ledger)], strategy_id="B4_ORB_SESSION_BREAKOUT", limit=25)
            r200 = build_shadow_demo_reconciliation(shadow, [("autotrade", ledger)], strategy_id="B4_ORB_SESSION_BREAKOUT", limit=200)

            self.assertTrue(r2["ok"])
            self.assertFalse(r2["degraded"])
            self.assertEqual(len(r2["signals"]), 2)
            self.assertEqual(len(r25["signals"]), 4)
            self.assertEqual(len(r200["signals"]), 4)

            for key in ("total_signals", "matched_both", "shadow_only", "demo_only", "skipped_both", "unverified"):
                self.assertEqual(r2["summary"][key], r25["summary"][key])
                self.assertEqual(r25["summary"][key], r200["summary"][key])

            self.assertEqual(r25["summary"]["total_signals"], 4)
            self.assertEqual(r25["summary"]["matched_both"], 1)
            self.assertEqual(r25["summary"]["shadow_only"], 3)

            by_sig = {s["signal_id"]: s for s in r25["signals"]}
            self.assertEqual(by_sig[b4_matched_sig]["pair_status"], "MATCHED_BOTH")
            for idx in range(3):
                rec_s = by_sig[f"sig-b4-skip-{idx}"]
                self.assertEqual(rec_s["demo"]["decision"], "SKIPPED")
                self.assertEqual(rec_s["demo"]["skip_reason"], "Лимит слотов портфеля")

    def test_reconciliation_source_failure_marks_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "demo.sqlite3")
            ts_ms = int(time.time() * 1000)
            ledger.intent(
                "at-b4-deg",
                "UNIUSDT",
                "BOOSTER:B4_ORB_SESSION_BREAKOUT",
                15.0,
                {
                    "side": "Buy",
                    "qty": 10.0,
                    "requestedQty": 10.0,
                    "signal_id": "sig-deg-1",
                    "signal_created_ms": ts_ms,
                    "entry": 10.0,
                    "sl": 9.5,
                    "tp": 11.5,
                    "orderIds": ["ord-deg-1"],
                },
            )
            ledger.finish("at-b4-deg", "FILLED", {"confirmedQty": 10.0, "orderIds": ["ord-deg-1"]})
            ledger.put_records(
                "executions",
                [
                    {
                        "execId": "ex-deg-1",
                        "orderId": "ord-deg-1",
                        "orderLinkId": "at-b4-deg",
                        "symbol": "UNIUSDT",
                        "side": "Buy",
                        "execType": "Trade",
                        "execQty": "10",
                        "closedSize": "0",
                        "execPrice": "10.0",
                        "execFee": "0.05",
                        "execTime": str(ts_ms + 100),
                    }
                ],
                "execId",
            )
            res = build_shadow_demo_reconciliation(_BrokenShadowEngine(), [("autotrade", ledger)], strategy_id="B4_ORB_SESSION_BREAKOUT")
            self.assertFalse(res["ok"])
            self.assertTrue(res["degraded"])
            self.assertFalse(res["sources"]["shadow"]["ok"])
            self.assertEqual(res["summary"]["demo_only"], 0)
            self.assertEqual(res["summary"]["unverified"], 1)
            self.assertEqual(res["signals"][0]["pair_status"], "UNVERIFIED_SHADOW_ERROR")
            self.assertEqual(res["signals"][0]["shadow"]["decision"], "SOURCE_UNAVAILABLE")

    def test_portfolio_risk_details_reports_incomplete_records(self) -> None:
        positions = [
            {"symbol": "BTCUSDT", "avgPrice": "70000", "stopLoss": "69000", "size": "0.01"},
            {"symbol": "ETHUSDT", "avgPrice": "3000", "stopLoss": "0", "size": "0.5"},
        ]
        orders = [
            {"symbol": "SOLUSDT", "price": "150", "stopLoss": "0", "qty": "2", "reduceOnly": False, "stopOrderType": ""},
            {"symbol": "BTCUSDT", "price": "69000", "stopLoss": "", "qty": "0.01", "reduceOnly": True, "stopOrderType": "StopLoss"},
        ]
        info = compute_portfolio_risk_details(positions, orders, strict=False)
        self.assertTrue(info["incomplete"])
        self.assertEqual(info["excluded_positions"], 1)
        self.assertEqual(info["excluded_orders"], 1)
        self.assertEqual(info["excluded_count"], 2)
        self.assertGreater(info["risk_usd"], 10.0)

    def test_emergency_stop_rechecks_positions_after_final_order_cancel(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st.entry_paused = threading.Event()
        st.logs = []
        st._logs_lock = threading.Lock()
        st.add_log = lambda msg: st.logs.append(msg)
        st.save_persisted_state = lambda: None
        ctrl = _MockEmergencyCtrl(new_pos_has_sl=True)
        st.bybit = ctrl
        st._active_controllers = lambda: [ctrl]

        res = st.execute_emergency_stop()
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "PARTIAL")
        self.assertEqual(res["accounts"][0]["remaining_positions"], 1)

        ctrl_unprot = _MockEmergencyCtrl(new_pos_has_sl=False)
        st.bybit = ctrl_unprot
        st._active_controllers = lambda: [ctrl_unprot]
        res_err = st.execute_emergency_stop()
        self.assertFalse(res_err["ok"])
        self.assertEqual(res_err["status"], "ERROR")
        self.assertEqual(res_err["accounts"][0]["unprotected_positions"], ["SOLUSDT"])

    def test_drawer_env_subaccount_selector_wired_in_ui(self) -> None:
        root = Path(__file__).resolve().parent.parent
        html = (root / "dashboard_report.html").read_text(encoding="utf-8")
        js = (root / "dashboard_app.js").read_text(encoding="utf-8")
        self.assertIn('id="env-key-account"', html)
        self.assertIn('value="booster"', html)
        self.assertIn('value="autotrade"', html)
        self.assertIn('id="env-sub-booster-masked"', html)
        self.assertIn('id="env-sub-autotrade-masked"', html)
        self.assertIn('action:"save_subaccount_keys"', js)
        self.assertIn("envSt.subaccounts", js)
        self.assertIn("portfolio_risk_incomplete", js)
        self.assertIn("UNVERIFIED", js)


if __name__ == "__main__":
    unittest.main()

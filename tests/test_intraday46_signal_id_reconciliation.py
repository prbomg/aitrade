# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.26-intraday.46: сквозной signal_id для сверки Shadow vs Bybit Demo."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aitrade.dashboard.live_server import ControlCenterState, create_wsgi_app
from aitrade.execution.audit_metrics import (
    build_shadow_demo_reconciliation,
    compute_live_strategy_metrics,
)
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.storage import Ledger


class TestIntraday46SignalIdReconciliation(unittest.TestCase):
    def test_shadow_engine_migrates_legacy_db_and_records_signal_id_and_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shadow_lab.sqlite3"
            # Создаём таблицу старой схемы (без signal_id и signal_created_ms)
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE shadow_trades (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_ts REAL NOT NULL,
                    closed_ts REAL DEFAULT 0,
                    max_hold_min INTEGER NOT NULL,
                    signal_entry REAL NOT NULL,
                    entry_px REAL NOT NULL,
                    sl_px REAL NOT NULL,
                    tp_px REAL NOT NULL,
                    exit_px REAL DEFAULT 0,
                    qty REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    risk_usd REAL NOT NULL,
                    fees_usd REAL DEFAULT 0,
                    pnl_usd REAL DEFAULT 0,
                    realized_r REAL DEFAULT 0,
                    exit_reason TEXT DEFAULT ''
                );
                """
            )
            conn.close()

            sh = LiveShadowEngine(db_path)
            sig1 = {
                "signal_id": "sig-b4-001",
                "signal_created_ms": 1700000000000,
                "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                "symbol": "UNIUSDT",
                "side": "Buy",
                "entry_price": 10.0,
                "stop_loss": 9.8,
                "take_profit": 10.6,
            }
            sig2_dup = dict(sig1, signal_id="sig-b4-002")
            opened = sh.record_signals([sig1, sig2_dup], {"UNIUSDT": (9.99, 10.01)}, now_ts=1700000001.0)
            self.assertEqual(opened, 1)

            detail = sh.get_strategy_detail("B4_ORB_SESSION_BREAKOUT", {"UNIUSDT": (10.1, 10.12)})
            self.assertEqual(detail["open_positions"][0]["signal_id"], "sig-b4-001")
            self.assertEqual(detail["open_positions"][0]["signal_created_ms"], 1700000000000)

            # Проверяем, что для второго дублирующего сигнала записано решение SKIPPED
            with sh._connect() as c:
                decs = c.execute(
                    "SELECT signal_id, decision, reason FROM shadow_signal_decisions ORDER BY id ASC"
                ).fetchall()
            self.assertEqual(len(decs), 2)
            self.assertEqual(decs[0][:2], ("sig-b4-001", "ENTERED"))
            self.assertEqual(decs[1][:2], ("sig-b4-002", "SKIPPED"))
            self.assertIn("уже открыта", decs[1][2])

    def test_reconciliation_joins_shadow_and_demo_by_signal_id_with_latency_and_exit_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sh = LiveShadowEngine(Path(tmp) / "shadow.sqlite3")
            ledger = Ledger(Path(tmp) / "demo.sqlite3")

            sig_created_ms = 1700000000000
            # Сигнал 1: вошли оба контура (Demo закрылся по TP, Shadow закрылся по TIME_STOP)
            sig_matched = {
                "signal_id": "sig-uni-match",
                "signal_created_ms": sig_created_ms,
                "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                "symbol": "UNIUSDT",
                "side": "Buy",
                "entry_price": 10.0,
                "stop_loss": 9.8,
                "take_profit": 10.6,
            }
            # Сигнал 2: Shadow вошёл, а Demo пропустил по лимиту слотов
            sig_shadow_only = {
                "signal_id": "sig-sol-skip",
                "signal_created_ms": sig_created_ms + 60_000,
                "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                "symbol": "SOLUSDT",
                "side": "Buy",
                "entry_price": 200.0,
                "stop_loss": 196.0,
                "take_profit": 212.0,
            }
            sh.record_signals(
                [sig_matched, sig_shadow_only],
                {"UNIUSDT": (9.99, 10.0), "SOLUSDT": (199.9, 200.0)},
                now_ts=sig_created_ms / 1000.0 + 1.0,
            )
            # Закрываем UNIUSDT в Shadow по таймеру
            sh.update_open_positions(
                {"UNIUSDT": (10.1, 10.12), "SOLUSDT": (201.0, 201.2)},
                now_ts=sig_created_ms / 1000.0 + 481 * 60.0,
            )

            # Записываем исполнение и закрытие по TP в Demo Ledger с тем же signal_id
            ledger.intent(
                "at-uni-1",
                "UNIUSDT",
                "BOOSTER:B4_ORB_SESSION_BREAKOUT",
                15.0,
                {
                    "side": "Buy",
                    "qty": 75.0,
                    "requestedQty": 75.0,
                    "signal_id": "sig-uni-match",
                    "signal_created_ms": sig_created_ms,
                    "entry": 10.0,
                    "sl": 9.8,
                    "tp": 10.6,
                    "orderId": "ord-entry-1",
                },
            )
            ledger.put_records(
                "executions",
                [
                    {
                        "execId": "ex-1",
                        "orderId": "ord-entry-1",
                        "orderLinkId": "at-uni-1",
                        "symbol": "UNIUSDT",
                        "side": "Buy",
                        "execType": "Trade",
                        "execQty": "75",
                        "closedSize": "0",
                        "execPrice": "9.99",
                        "execFee": "0.15",
                        "execTime": str(sig_created_ms + 2400),
                    },
                    {
                        "execId": "ex-2",
                        "orderId": "ord-exit-1",
                        "orderLinkId": "",
                        "symbol": "UNIUSDT",
                        "side": "Sell",
                        "execType": "Trade",
                        "execQty": "75",
                        "closedSize": "75",
                        "execPrice": "10.60",
                        "execFee": "0.16",
                        "stopOrderType": "TakeProfit",
                        "execTime": str(sig_created_ms + 3600_000),
                    },
                ],
                "execId",
            )
            ledger.put_records(
                "closed",
                [
                    {
                        "orderId": "ord-exit-1",
                        "symbol": "UNIUSDT",
                        "side": "Sell",
                        "qty": "75",
                        "closedSize": "75",
                        "avgEntryPrice": "9.99",
                        "avgExitPrice": "10.60",
                        "closedPnl": "45.44",
                        "updatedTime": str(sig_created_ms + 3600_000),
                    }
                ],
                "orderId",
            )
            ledger.event(
                "SIGNAL_DECISION",
                {
                    "signal_id": "sig-uni-match",
                    "signal_created_ms": sig_created_ms,
                    "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                    "contour": "BOOSTER",
                    "symbol": "UNIUSDT",
                    "side": "Buy",
                    "decision": "SUBMITTED",
                    "reason": "",
                    "intent_id": "at-uni-1",
                },
            )
            ledger.event(
                "SIGNAL_DECISION",
                {
                    "signal_id": "sig-sol-skip",
                    "signal_created_ms": sig_created_ms + 60_000,
                    "strategy_id": "B4_ORB_SESSION_BREAKOUT",
                    "contour": "BOOSTER",
                    "symbol": "SOLUSDT",
                    "side": "Buy",
                    "decision": "SKIPPED",
                    "reason": "Нет свободных слотов Разгона (лимит 12)",
                    "intent_id": "",
                },
            )

            live_m = compute_live_strategy_metrics([("autotrade", ledger)])
            tr = live_m["execution_traces"][0]
            self.assertEqual(tr["signal_id"], "sig-uni-match")
            self.assertEqual(tr["fill_latency_ms"], 2400)
            self.assertEqual(tr["exit_reason"], "TP")

            rec = build_shadow_demo_reconciliation(sh, [("autotrade", ledger)], strategy_id="B4_ORB_SESSION_BREAKOUT")
            self.assertEqual(rec["summary"]["total_signals"], 2)
            self.assertEqual(rec["summary"]["matched_both"], 1)
            self.assertEqual(rec["summary"]["matched_closed"], 1)
            self.assertEqual(rec["summary"]["shadow_only"], 1)

            by_sig = {row["signal_id"]: row for row in rec["signals"]}
            uni_row = by_sig["sig-uni-match"]
            self.assertEqual(uni_row["pair_status"], "MATCHED_BOTH")
            self.assertEqual(uni_row["shadow"]["trade"]["exit_reason"], "TIME_STOP")
            self.assertEqual(uni_row["demo"]["exit_reason"], "TP")
            self.assertEqual(uni_row["demo"]["fill_latency_ms"], 2400)

            sol_row = by_sig["sig-sol-skip"]
            self.assertEqual(sol_row["pair_status"], "SHADOW_ONLY")
            self.assertEqual(sol_row["demo"]["decision"], "SKIPPED")
            self.assertIn("Нет свободных слотов", sol_row["demo"]["skip_reason"])

            # Проверяем HTTP-эндпоинт /api/signal_reconciliation
            st = ControlCenterState.__new__(ControlCenterState)
            st.shadow_engine = sh
            st.bybit = MagicMock()
            st.bybit.ledger = ledger
            st.booster_bybit = st.bybit
            app = create_wsgi_app(st)
            captured = {}

            def start_response(status, headers):
                captured["status"] = status

            body = b"".join(
                app(
                    {
                        "REQUEST_METHOD": "GET",
                        "PATH_INFO": "/api/signal_reconciliation",
                        "QUERY_STRING": "strategy=B4_ORB_SESSION_BREAKOUT&limit=50",
                    },
                    start_response,
                )
            )
            self.assertEqual(captured["status"], "200 OK")
            parsed = json.loads(body.decode("utf-8"))
            self.assertTrue(parsed["ok"])
            self.assertEqual(parsed["summary"]["matched_closed"], 1)


if __name__ == "__main__":
    unittest.main()

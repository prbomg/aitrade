# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.24-intraday.41: фиксы отказоустойчивости 24/7 (пункты 1–7 аудита)."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from aitrade.dashboard.live_server import BybitV5DirectController, ControlCenterState
from aitrade.storage import Ledger


class TestIntraday41AuditResilienceFixes(unittest.TestCase):
    def test_stale_uncertain_intent_auto_clears_when_exchange_has_no_open_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "ledger.sqlite3", environment="demo")
            ctrl = BybitV5DirectController.__new__(BybitV5DirectController)
            ctrl.ledger = ledger
            ctrl._entry_lock = threading.RLock()
            ctrl.spm = MagicMock()
            ctrl.spm.paged_get.return_value = []
            ctrl.spm.signed_get.return_value = {"result": {"list": []}}

            # Свежий UNCERTAIN (создан только что) НЕ должен сбрасываться
            ledger.intent("at-fresh", "BTCUSDT", "CORE:T1_DONCHIAN_TSMOM", 10.0, {"side": "Buy", "qty": 0.01})
            ledger.finish("at-fresh", "UNCERTAIN", {"error": "pre-journal network error"})
            ctrl.reconcile_pending()
            self.assertTrue(ledger.unresolved("BTCUSDT"))

            # Протухший UNCERTAIN (>30 мин) без открытых ордеров и позиций на бирже должен очиститься
            stale_ts = time.time() - (ctrl.UNCERTAIN_STALE_TTL_SEC + 60.0)
            with ledger.connect() as c:
                c.execute("UPDATE intents SET created=? WHERE id=?", (stale_ts, "at-fresh"))
            ctrl.reconcile_pending()
            self.assertFalse(ledger.unresolved("BTCUSDT"))
            with ledger.connect() as c:
                st = c.execute("SELECT status FROM intents WHERE id=?", ("at-fresh",)).fetchone()[0]
            self.assertEqual(st, "REJECTED")

    def test_stale_uncertain_intent_preserved_if_position_exists_on_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "ledger.sqlite3", environment="demo")
            ctrl = BybitV5DirectController.__new__(BybitV5DirectController)
            ctrl.ledger = ledger
            ctrl._entry_lock = threading.RLock()
            ctrl.spm = MagicMock()
            # Ордеров нет, но на бирже есть открытая позиция по BTCUSDT
            ctrl.spm.paged_get.side_effect = lambda path, params=None: (
                [{"symbol": "BTCUSDT", "size": "0.02"}] if "position/list" in path else []
            )
            ledger.intent("at-open", "BTCUSDT", "CORE:T1_DONCHIAN_TSMOM", 10.0, {"side": "Buy", "qty": 0.02})
            ledger.finish("at-open", "UNCERTAIN", {"error": "ambiguous"})
            stale_ts = time.time() - (ctrl.UNCERTAIN_STALE_TTL_SEC + 60.0)
            with ledger.connect() as c:
                c.execute("UPDATE intents SET created=? WHERE id=?", (stale_ts, "at-open"))

            ctrl.reconcile_pending()
            self.assertTrue(ledger.unresolved("BTCUSDT"))

    def test_refresh_funding_rates_logs_on_error_and_updates_on_success(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st.logs = []
        st._logs_lock = threading.Lock()
        st.funding_rates = {}
        st.funding_updated_at = 0.0
        st.bybit = MagicMock()
        st.bybit.ledger = MagicMock()
        st.engine = MagicMock()

        st.engine.client.get_tickers.side_effect = RuntimeError("bybit 502")
        self.assertFalse(st.refresh_funding_rates())
        self.assertTrue(any("фандинга" in x for x in st.logs))

        st.engine.client.get_tickers.side_effect = None
        st.engine.client.get_tickers.return_value = [{"symbol": "BTCUSDT", "fundingRate": "0.0001"}]
        self.assertTrue(st.refresh_funding_rates())
        self.assertAlmostEqual(st.funding_rates["BTCUSDT"], 0.0001)
        self.assertGreater(st.funding_updated_at, 0.0)

    def test_get_closed_pnl_reconcile_false_skips_reconcile_pending(self) -> None:
        ctrl = BybitV5DirectController.__new__(BybitV5DirectController)
        ctrl._history_lock = threading.RLock()
        ctrl._history_ts = time.time()
        ctrl.ledger = MagicMock()
        ctrl.ledger.records.return_value = []
        ctrl.ledger.attribute.return_value = []
        ctrl.reconcile_pending = MagicMock()

        ctrl.get_closed_pnl(reconcile=False)
        ctrl.reconcile_pending.assert_not_called()

        ctrl.get_closed_pnl(reconcile=True)
        ctrl.reconcile_pending.assert_called_once()

    def test_close_position_with_lock_timeout_does_not_deadlock(self) -> None:
        ctrl = BybitV5DirectController.__new__(BybitV5DirectController)
        ctrl._entry_lock = threading.RLock()
        ctrl._close_position_locked = MagicMock(return_value={"ok": True, "status": "PENDING"})

        locked = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            with ctrl._entry_lock:
                locked.set()
                release.wait(timeout=5.0)

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        self.assertTrue(locked.wait(timeout=2.0))
        try:
            t0 = time.monotonic()
            res = ctrl.close_position("BTCUSDT", "Buy", "0.01", 0, lock_timeout=0.05)
            elapsed = time.monotonic() - t0
            self.assertTrue(res["ok"])
            self.assertLess(elapsed, 1.0)
        finally:
            release.set()
            t.join(timeout=2.0)

    def test_get_all_positions_merges_isolated_booster_and_autotrade(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st.bybit = MagicMock()
        st.bybit.get_positions.return_value = [{"symbol": "BTCUSDT", "side": "Buy"}]
        st.booster_bybit = MagicMock()
        st.booster_bybit.get_positions.return_value = [{"symbol": "ETHUSDT", "side": "Sell"}]

        merged = st._get_all_positions()
        self.assertEqual([p["symbol"] for p in merged], ["BTCUSDT", "ETHUSDT"])

    def test_logs_and_ui_toast_guard_warning(self) -> None:
        st = ControlCenterState.__new__(ControlCenterState)
        st.logs = []
        st._logs_lock = threading.Lock()
        st.bybit = MagicMock()
        st.bybit.ledger = MagicMock()
        for i in range(135):
            st.add_log(f"msg-{i}")
        snap = st._get_logs_copy()
        self.assertEqual(len(snap), 120)
        self.assertIn("msg-134", snap[0])

        js_text = Path(__file__).resolve().parents[1].joinpath("dashboard_app.js").read_text(encoding="utf-8")
        self.assertIn("if (r.retMsg && r.entry_paused)", js_text)


if __name__ == "__main__":
    unittest.main()

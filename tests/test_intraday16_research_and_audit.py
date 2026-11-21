# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from aitrade.exchange.bybit_v5 import Candle
from aitrade.storage import Ledger
from aitrade.strategies.extreme_flush_mr import generate_extreme_flush_mr_signal
from scripts.live_audit_report import build_live_audit_summary


class TestIntraday16ResearchAndAudit(unittest.TestCase):
    def test_mr1_extreme_flush_long_and_neutral(self):
        hour_ms = 3_600_000
        start_ts = 1_750_000_000_000
        # 34 спокойных 1h бара вокруг 100.0
        bars = [
            Candle(start_ts + i * hour_ms, 100.0, 100.4, 99.6, 100.0 + (0.15 if i % 2 == 0 else -0.15), 1000.0, 100000.0)
            for i in range(34)
        ]
        # Нейтральный рынок -> сигнала нет
        self.assertIsNone(generate_extreme_flush_mr_signal("BTCUSDT", bars, reference_open_px=100.0))

        # Резкий каскадный пролив (-5%) с нижним хвостом выкупа
        bars_flush = list(bars) + [
            Candle(start_ts + 34 * hour_ms, 99.8, 99.9, 93.0, 95.8, 5000.0, 480000.0)
        ]
        sig = generate_extreme_flush_mr_signal(
            "BTCUSDT", bars_flush, reference_open_px=95.9, funding_rate=-0.0001
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "MR1_EXTREME_FLUSH")
        self.assertEqual(sig.side, "Buy")
        self.assertLess(sig.stop_loss, sig.entry_price)
        self.assertGreater(sig.take_profit_2, sig.entry_price)

    def test_live_audit_summary_reads_wfa_and_blocked_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "wfa_validation.json").write_text(
                json.dumps({
                    "strategies": {
                        "T1_DONCHIAN_TSMOM": {"wfe_passed": True, "oos_pf": 1.55, "oos_2x_avg_r": 0.168}
                    }
                }),
                encoding="utf-8",
            )
            ledger = Ledger(d / "execution_ledger_autotrade.sqlite3")
            ledger.event("ENTRY_BLOCKED", {"strategy": "CORE:T1_DONCHIAN_TSMOM", "reason": "Лимит позиций"})
            rep = build_live_audit_summary(d)
            self.assertIn("T1_DONCHIAN_TSMOM", rep["strategies"])
            self.assertTrue(rep["strategies"]["T1_DONCHIAN_TSMOM"]["wfa_passed"])
            self.assertEqual(rep["top_blocked_reasons"][0][1], 1)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aitrade.booster.oi_squeeze_scanner import OISqueezeScanner
from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.execution.limit_chaser import SmartLimitChaser
from aitrade.execution.smart_position_manager import SmartPositionManager


class TestIntraday52ExecutionConcurrency(unittest.TestCase):
    def test_spm_get_spec_multidomain_fallback_and_cache(self):
        spm = SmartPositionManager("k", "s", base_url="https://api-demo.bybit.com")
        calls = []

        def fake_urlopen(url, timeout=8):
            calls.append(url)
            if "api-demo.bybit.com" in url:
                raise OSError("502 Bad Gateway")
            payload = {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "status": "Trading",
                            "lotSizeFilter": {
                                "qtyStep": "0.001",
                                "minOrderQty": "0.001",
                                "maxOrderQty": "100",
                                "minNotionalValue": "5",
                            },
                            "priceFilter": {"tickSize": "0.1"},
                        }
                    ]
                },
            }
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            spec1 = spm.get_spec("BTCUSDT")
            spec2 = spm.get_spec("BTCUSDT")

        self.assertEqual(spec1["qty_step"], 0.001)
        self.assertEqual(spec1["tick_size"], 0.1)
        self.assertEqual(spec1, spec2)
        # Первый домен упал, второй отдал спецификацию, второй вызов взят из кэша
        self.assertEqual(len(calls), 2)

    def test_limit_chaser_isolated_stats_filename_and_tpsl_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            spm = SmartPositionManager("k", "s")
            ch = SmartLimitChaser(spm, None, Path(tmp), stats_filename="chaser_stats_booster.json")
            self.assertEqual(ch.stats_path.name, "chaser_stats_booster.json")
            self.assertTrue(hasattr(ch, "_lock"))
            with self.assertRaises(ValueError):
                ch.execute_chased_entry("BTCUSDT", "Buy", 0.01, 100000.0, take_profit=None, stop_loss=99000.0)
            with self.assertRaises(ValueError):
                ch.execute_chased_entry("BTCUSDT", "Buy", 0.01, 100000.0, take_profit=102000.0, stop_loss=0.0)

    def test_scanners_have_thread_locks(self):
        oi = OISqueezeScanner()
        xr = XNewsSentimentRadar()
        self.assertTrue(hasattr(oi, "_lock"))
        self.assertTrue(hasattr(xr, "_lock"))
        s = xr.get_summary()
        self.assertEqual(s["global_state"], "NEUTRAL")


if __name__ == "__main__":
    unittest.main()

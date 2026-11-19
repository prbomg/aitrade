# -*- coding: utf-8 -*-
"""Тесты для привязки WARMING_UP к wfa_validation.json и разделения account/TP1 в get_full_state (intraday.9)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.config import load_settings
from aitrade.dashboard.live_server import ControlCenterState
from aitrade.exchange.bybit_v5 import BybitV5Client
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.strategies.base import TradeSignal


class Intraday9ValidationAndAccountsTests(unittest.TestCase):
    def test_get_full_state_isolates_account_and_tp1_orders(self):
        st = ControlCenterState.__new__(ControlCenterState)
        st.last_scan_ts = "12:00:00"
        st.autopilot_booster_enabled = False
        st.autopilot_core_enabled = False
        st.autopilot_interval_sec = 300
        st.booster_cycles_done = 0
        st.core_cycles_done = 0
        st._last_auto_ts = 0.0
        st.smart_guard_enabled = True
        st.split_tp_enabled = True
        st.max_leverage = 3
        st.core_max_positions = 3
        st.core_min_prob_pct = 58.0
        st.sound_alerts_enabled = False
        st.manual_prefs = {}
        st.last_scan = {}
        st.backtest_cache = {}
        st.logs = []
        st.settings = load_settings()
        st.ws_hub = MagicMock()
        st.ws_hub.get_summary.return_value = {}
        st.regime_guard = MagicMock()
        st.regime_guard.update_btc_regime.return_value = {}
        st.regime_guard.get_sector.return_value = "L1"
        st.env_manager = MagicMock()
        st.env_manager.get_public_status.return_value = {}
        st.x_radar = MagicMock()
        st.x_radar.get_summary.return_value = {}
        st.ml_calibrator = MagicMock()
        st.ml_calibrator.get_summary.return_value = {}
        st.oi_scanner = MagicMock(cached_signals=[])
        st.limit_chaser = MagicMock()
        st.limit_chaser.get_summary.return_value = {}
        st.booster = MagicMock()
        st.booster.compute_metrics.return_value = {"vault_usd": 0.0}
        st.get_position_age_minutes = MagicMock(return_value=10.0)

        st.bybit = MagicMock()
        st.bybit.spm = MagicMock(breakeven_trigger_pct=0.55, trailing_step_pct=0.40)
        st.bybit.get_account_summary.return_value = {"usdt_balance": 5000.0}
        st.bybit.get_closed_pnl.return_value = []
        st.bybit.get_positions.return_value = [
            {"symbol": "BTCUSDT", "side": "Buy", "positionIdx": 0, "avgPrice": "60000", "stopLoss": "59000", "trailingStop": "0"}
        ]
        # У Core только условный StopLoss (НЕ TP1!)
        st.bybit.get_open_orders.return_value = [
            {"symbol": "BTCUSDT", "positionIdx": 0, "reduceOnly": True, "orderType": "Market", "stopOrderType": "StopLoss", "price": "59000", "qty": "0.1"}
        ]

        st.booster_bybit = MagicMock()
        st.booster_bybit.get_account_summary.return_value = {"usdt_balance": 1000.0}
        st.booster_bybit.get_closed_pnl.return_value = []
        st.booster_bybit.get_positions.return_value = [
            {"symbol": "BTCUSDT", "side": "Buy", "positionIdx": 0, "avgPrice": "60100", "stopLoss": "59100", "trailingStop": "0"}
        ]
        # У Booster есть реальный Limit reduceOnly TP1
        st.booster_bybit.get_open_orders.return_value = [
            {"symbol": "BTCUSDT", "positionIdx": 0, "reduceOnly": True, "orderType": "Limit", "stopOrderType": "", "price": "62000", "qty": "0.05"}
        ]

        full = st.get_full_state()
        self.assertEqual(full["booster_account"]["usdt_balance"], 1000.0)
        core_p = next(p for p in full["positions"] if p["account"] == "autotrade")
        boost_p = next(p for p in full["positions"] if p["account"] == "booster")
        # Core не должен подцепить ни свой StopLoss, ни TP1 из Booster
        self.assertEqual(core_p["tp1_order_price"], "")
        self.assertEqual(boost_p["tp1_order_price"], "62000")

    def test_warming_up_respects_wfa_validation_artifact(self):
        settings = load_settings()
        ml = WalkForwardMetaLabeler(settings)
        cli = BybitV5Client(settings, allow_offline_fallback=True)
        c15 = cli._synthetic_klines("BTCUSDT", "15", 80)
        ctx = MagicMock()
        ctx.candles_15m = c15
        ctx.screener = MagicMock(spread_pct=0.02)

        sig = TradeSignal(
            symbol="BTCUSDT",
            strategy_id="S3_VWAP_RANGE",
            strategy_name="S3",
            timeframe="15",
            side="Buy",
            order_type="Limit",
            entry_price=100.0,
            stop_loss=99.0,
            take_profit_1=101.5,
            take_profit_2=101.5,
            base_winrate_est=0.55,
            features={"post_only": 1.0},
        )
        with tempfile.TemporaryDirectory() as td:
            val_file = Path(td) / "wfa_validation.json"
            val_file.write_text(json.dumps({"strategies": {"S3_VWAP_RANGE": {"wfe_passed": False}}}), encoding="utf-8")
            with patch.object(WalkForwardMetaLabeler, "WFA_VALIDATION_PATH", val_file):
                out_fail = ml.evaluate_signal(sig, ctx)
                self.assertFalse(out_fail.passed_ml_filter)
                self.assertEqual(out_fail.features["ml_calibration_status"], "WARMING_UP_BLOCKED_BY_WFA")

            val_file.write_text(json.dumps({"strategies": {"S3_VWAP_RANGE": {"wfe_passed": True}}}), encoding="utf-8")
            with patch.object(WalkForwardMetaLabeler, "WFA_VALIDATION_PATH", val_file):
                out_ok = ml.evaluate_signal(sig, ctx)
                self.assertTrue(out_ok.passed_ml_filter)
                self.assertEqual(out_ok.features["ml_calibration_status"], "WARMING_UP")


if __name__ == "__main__":
    unittest.main()

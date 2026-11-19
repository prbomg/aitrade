# -*- coding: utf-8 -*-
"""Негативные и регрессионные тесты по результатам живого аудита intraday.5 -> intraday.6."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from aitrade.agents.debate_council import MultiAgentDebateCouncil
from aitrade.backtest.walk_forward import WalkForwardBacktester, compute_daily_sharpe_and_dsr
from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.config import load_settings
from aitrade.dashboard.live_server import BybitV5DirectController, ControlCenterState
from aitrade.exchange.bybit_v5 import BybitV5Client, Candle
from aitrade.exchange.transport import ExchangeError
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.strategies.base import TradeSignal
from aitrade.strategies.regime_router import classify_regime


class Intraday6AuditRegressionTests(unittest.TestCase):
    def test_wfe_negative_over_negative_never_passes_and_dsr_scale_invariant(self):
        # 1. DSR должен быть строго инвариантен к масштабированию доходностей (плечу)
        rets = [0.004, -0.002, 0.005, 0.001, -0.001, 0.006, 0.003, -0.002, 0.004, 0.002]
        sr1, dsr1 = compute_daily_sharpe_and_dsr(rets, n_trials=4)
        sr10, dsr10 = compute_daily_sharpe_and_dsr([r * 10.0 for r in rets], n_trials=4)
        self.assertEqual(sr1, sr10)
        self.assertEqual(dsr1, dsr10)

        # 2. Убыточный IS и убыточный OOS не могут давать wfe_passed=True
        bt = WalkForwardBacktester(load_settings())
        base_ts = 1_700_000_000_000
        step_ms = 900_000
        bars = [
            Candle(base_ts + i * step_ms, 100.0, 101.0, 99.0, 100.0, 1000.0, 100000.0)
            for i in range(600)
        ]
        # Искусственно возвращаем убыточные сделки на всех окнах
        bad_trades = [
            (base_ts + (250 + k * 20) * step_ms, -1.5, -1.0, "S3_VWAP_RANGE", base_ts + (260 + k * 20) * step_ms, "BTCUSDT", 0.01)
            for k in range(15)
        ]
        bt._simulate_s1_s3_replay_dual = Mock(return_value=(bad_trades, bad_trades))
        res = bt.run_rolling_wfa({"BTCUSDT": bars}, n_cycles=4, mc_iterations=100)
        self.assertIsNone(res["wfe"])
        self.assertFalse(res["wfe_passed"])

    def test_x_news_etf_outflows_is_bearish_and_emergency_close_disabled(self):
        radar = XNewsSentimentRadar()
        score, trig, _ = radar._score_text("Spot Bitcoin ETF outflows reach $250M today")
        self.assertLess(score, 0)
        self.assertNotIn("+etf", trig)

        # Глобальный PANIC не должен закрывать несвязанный актив по умолчанию
        radar.panic_active = True
        radar.panic_reason = "Lawyers discuss hack recovery options"
        radar.block_on_panic = True
        ev = radar.evaluate_open_position("NEARUSDT", "Buy")
        self.assertEqual(ev["action"], "HOLD")

    def test_max_hold_closes_expired_position_despite_untriggered_sl_tp(self):
        st = ControlCenterState.__new__(ControlCenterState)
        st.smart_guard_enabled = True
        st.split_tp_enabled = False
        st.entry_paused = threading.Event()
        st.logs = []
        st.add_log = Mock()
        st.position_opened_ts = {}
        st.x_radar = Mock(enabled=False)
        st.regime_guard = Mock(time_stop_enabled=False)

        old_ms = int((time.time() - 500 * 60) * 1000)
        ctrl = MagicMock()
        ctrl.spm = MagicMock()
        ctrl.spm.audit_and_protect_open_positions.return_value = []
        # Условный SL/TP (Untriggered, stopOrderType="StopLoss") не должен блокировать max_hold
        ctrl.get_open_orders.return_value = [
            {
                "symbol": "BTCUSDT",
                "reduceOnly": True,
                "orderType": "Market",
                "stopOrderType": "StopLoss",
                "orderStatus": "Untriggered",
                "orderId": "sl-1",
            }
        ]
        ctrl.get_positions.return_value = [
            {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "size": "0.01",
                "avgPrice": "60000",
                "markPrice": "60100",
                "stopLoss": "59000",
                "positionIdx": 0,
                "entry_intent_id": "at-123",
                "opened_ms": old_ms,
                "strategy_id": "CORE:S3_VWAP_RANGE",
            }
        ]
        ctrl.ledger.active_intent.return_value = {"exit_plan": {"max_hold_minutes": 240}}
        ctrl.close_position.return_value = {"ok": True}

        st._run_guard_cycle_for_controller(ctrl)
        ctrl.close_position.assert_called_once_with("BTCUSDT", "Buy", "0.01", 0)

    def test_debate_summary_never_says_approved_when_rejected(self):
        settings = load_settings()
        council = MultiAgentDebateCouncil(settings)
        cli = BybitV5Client(settings, allow_offline_fallback=True)
        c15 = cli._synthetic_klines("NEARUSDT", "15", 80)
        ob = cli._synthetic_orderbook("NEARUSDT", 20)
        ctx = MagicMock()
        ctx.candles_15m = c15
        ctx.vwap_bands = MagicMock(vwap=c15[-1].close, std=0.1)
        ctx.screener = MagicMock(funding_rate=0.0001, mtf_bias="NEUTRAL", rvol=1.2, natr_pct=1.0, adx_1h=20.0)
        ctx.micro = MagicMock(smoothed_obi=0.1, spoofing_flags=[], vpin_tof=0.2, liquidity_behavior_index=0.9)

        sig = TradeSignal(
            symbol="NEARUSDT",
            strategy_id="AUTOTRADE_MOMENTUM_FUNDING",
            strategy_name="Momentum",
            timeframe="60",
            side="Buy",
            order_type="Limit",
            entry_price=5.0,
            stop_loss=4.9,
            take_profit_1=5.2,
            take_profit_2=5.3,
            base_winrate_est=0.5,
            passed_ml_filter=False,
        )
        out = council.conduct_debate(sig, ctx)
        self.assertEqual(out.debate_verdict, "REJECTED")
        self.assertIn("ОТКЛОНЕНО", out.debate_summary)
        self.assertNotIn("ОДОБРЕНО", out.debate_summary)

    def test_regime_router_no_cross_series_cache_collision(self):
        t0 = 1_700_000_000_000
        step = 3_600_000
        # Серия 1: явный тренд вверх
        trend = []
        px = 100.0
        for i in range(120):
            o = px
            c = px + 1.0
            trend.append(Candle(t0 + i * step, o, c + 0.3, o - 0.1, c, 1000.0, 100000.0))
            px = c
        # Серия 2: флэт с теми же граничными таймстемпами, длиной и последним close
        flat = []
        for i in range(120):
            base = 220.0 + (0.2 if i % 2 == 0 else -0.2)
            c = trend[-1].close if i == 119 else base
            flat.append(Candle(t0 + i * step, base, base + 0.3, base - 0.3, c, 1000.0, 100000.0))

        d1 = classify_regime(trend)
        d2 = classify_regime(flat)
        self.assertEqual(d1.regime, "TREND_UP")
        self.assertNotEqual(d2.regime, "TREND_UP")

    def test_meta_labeler_momentum_has_no_synthetic_atr_samples(self):
        settings = load_settings()
        ml = WalkForwardMetaLabeler(settings)
        cli = BybitV5Client(settings, allow_offline_fallback=True)
        c15 = cli._synthetic_klines("BTCUSDT", "15", 300)
        X, y, ret = ml._extract_historical_matrix(c15, 1.0, strategy_id="AUTOTRADE_MOMENTUM_FUNDING")
        self.assertEqual(X, [])
        self.assertEqual(y, [])
        self.assertEqual(ret, [])

    def test_ensure_isolated_margin_uta2_fallback_and_place_checked_blocks_on_failure(self):
        spm = SmartPositionManager("k", "s")
        # 1. На UTA 2.0 switch-isolated кидает 100028 -> фолбэк на /v5/account/set-margin-mode
        def side_eff(ep, body):
            if ep == "/v5/position/switch-isolated":
                raise ExchangeError("UTA 2.0 unsupported", code=100028)
            if ep == "/v5/account/set-margin-mode":
                return {"retCode": 0, "retMsg": "OK"}
            return {"retCode": 0}

        spm.signed_post = Mock(side_effect=side_eff)
        res = spm.ensure_isolated_margin("BTCUSDT", 3)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "ISOLATED_MARGIN")

        # 2. Если ensure_isolated_margin вернул ok=False, _place_checked должен отклонить заявку
        with tempfile.TemporaryDirectory() as td:
            eng = MagicMock()
            eng.settings = load_settings()
            eng.client.api_key = "k"
            eng.client.api_secret = "s"
            c = BybitV5DirectController(eng)
            from aitrade.storage import Ledger

            c.ledger = Ledger(Path(td) / "l.sqlite3")
            c.spm = MagicMock(
                get_spec=Mock(return_value={"qty_step": 0.001, "min_qty": 0.001, "max_qty": 100.0, "min_notional": 5.0, "tick_size": 0.1}),
                fmt_step=SmartPositionManager.fmt_step,
                ensure_isolated_margin=Mock(return_value={"ok": False, "retMsg": "margin rejected"}),
            )
            c.get_positions = Mock(return_value=[])
            c.get_open_orders = Mock(return_value=[])
            c.get_closed_pnl = Mock(return_value=[])
            c.get_account_summary = Mock(return_value={"usdt_balance": 1000.0})
            owner = MagicMock()
            owner.entry_paused = threading.Event()
            owner.ws_hub.get_bbo.return_value = (100.0, 100.02)
            owner.ws_hub.get_summary.return_value = {"flow_window_complete": True, "symbols_live": 10}
            owner.funding_rates = {"BTCUSDT": 0.0001}
            owner.funding_updated_at = time.time()
            owner.regime_guard.can_open_position.return_value = (True, "OK")
            owner.booster = None
            owner.check_autotrade_drawdown = Mock(return_value=(1.0, False))
            c.owner = owner

            out = c.place_order("BTCUSDT", "Buy", "Limit", 0.5, 100.0, 102.0, 99.0, 3, False, "CORE:S3_VWAP_RANGE")
            self.assertFalse(out["ok"])
            self.assertIn("Isolated Margin", out["retMsg"])


if __name__ == "__main__":
    unittest.main()

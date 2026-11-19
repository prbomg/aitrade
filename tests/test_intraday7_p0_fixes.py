# -*- coding: utf-8 -*-
"""Юнит-тесты для всех P0-1..P0-6 исправлений релиза intraday.7."""

import math
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

from aitrade.backtest.walk_forward import WalkForwardBacktester, compute_daily_sharpe_and_dsr
from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.config import load_settings
from aitrade.dashboard.live_server import ControlCenterState
from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_hurst_exponent
from aitrade.strategies.regime_router import classify_regime


class Intraday7P0FixesTests(unittest.TestCase):
    def test_p0_1_portfolio_admits_booster_4pct_risk_and_dsr_noise_is_low(self):
        bt = WalkForwardBacktester(load_settings())
        t0 = 1_700_000_000_000
        trades = [
            (t0 + k * 86_400_000, 3.0, 2.0, "B1_SQUEEZE_BREAKOUT", t0 + k * 86_400_000 + 3_600_000, "BTCUSDT", 0.015)
            for k in range(5)
        ]
        pnls, eq, dd, skipped = bt._portfolio(
            trades,
            risk_per_trade_frac=0.04,
            max_total_risk_frac=0.07,
            max_leverage=5.0,
            max_positions=2,
            allow_compounding=True,
        )
        self.assertEqual(len(pnls), 5)
        self.assertEqual(skipped, 0)
        self.assertGreater(eq, 1000.0)

        # Слабый шумовой ряд (n=180, n_trials=100) должен получать DSR < 0.05
        rng = random.Random(7)
        noise = [rng.gauss(0.0002, 0.01) for _ in range(180)]
        _, dsr = compute_daily_sharpe_and_dsr(noise, n_trials=100)
        self.assertIsNotNone(dsr)
        self.assertLess(dsr, 0.05)

    def test_p0_2_ratchet_vault_6_winning_steps_never_triggers_fake_dd(self):
        with tempfile.TemporaryDirectory() as td:
            bm = DepositBoosterManager(Path(td), isolated_subaccount=True)
            now_ms = int(time.time() * 1000)
            bm.state["started_at_ms"] = now_ms - 10_000
            closed = []
            prev_vault = 0.0
            for step in range(1, 7):
                m_before = bm.compute_metrics(closed, [])
                win_pnl = round(m_before["current_equity"] * 0.55, 2)
                closed.append({
                    "orderId": f"w-{step}",
                    "symbol": "BTCUSDT",
                    "strategy_id": "BOOSTER:B1_SQUEEZE_BREAKOUT",
                    "campaign_id": str(bm.state["started_at_ms"]),
                    "updatedTime": now_ms + step * 1000,
                    "closedPnl": win_pnl,
                })
                m = bm.compute_metrics(closed, [])
                self.assertGreater(m["vault_usd"], prev_vault)
                prev_vault = m["vault_usd"]
                self.assertEqual(m["dd_from_hwm_pct"], 0.0)
                self.assertFalse(m["near_floor_protection"])
                self.assertFalse(m["circuit_breaker_tripped"])
                self.assertEqual(m["base_risk_pct"], 4.0)

    def test_p0_3_unbiased_hurst_random_walk_and_gbm_trend(self):
        # 1. 100 случайных блужданий -> среднее H в [0.47, 0.53]
        h_vals = []
        for seed in range(100):
            rng = random.Random(seed)
            px = 100.0
            series = [px]
            for _ in range(200):
                px *= math.exp(rng.gauss(0.0, 0.01))
                series.append(px)
            h_vals.append(calc_hurst_exponent(series, window=160))
        mean_h = sum(h_vals) / len(h_vals)
        self.assertGreaterEqual(mean_h, 0.47)
        self.assertLessEqual(mean_h, 0.53)

        # 2. 100 зашумлённых GBM-трендов (+30% за 160 баров) -> >= 65% классифицируются как TREND_UP
        t0 = 1_700_000_000_000
        trend_hits = 0
        for seed in range(100):
            rng = random.Random(1000 + seed)
            px = 100.0
            bars = []
            for i in range(180):
                o = px
                ret = 0.0018 + rng.gauss(0.0, 0.006)
                c = o * math.exp(ret)
                h = max(o, c) * (1.0 + abs(rng.gauss(0.0, 0.002)))
                l = min(o, c) * (1.0 - abs(rng.gauss(0.0, 0.002)))
                bars.append(Candle(t0 + i * 3_600_000, o, h, l, c, 1000.0, 100000.0))
                px = c
            if classify_regime(bars).regime == "TREND_UP":
                trend_hits += 1
        self.assertGreaterEqual(trend_hits, 65)

    def test_p0_6_limit_ttl_cancels_stale_s3_and_momentum_orders(self):
        st = ControlCenterState.__new__(ControlCenterState)
        st.smart_guard_enabled = True
        st.split_tp_enabled = False
        st.entry_paused = threading.Event()
        st.logs = []
        st.add_log = Mock()
        st.position_opened_ts = {}
        st.x_radar = Mock(enabled=False)
        st.regime_guard = Mock(time_stop_enabled=False)

        now_ms = int(time.time() * 1000)
        ctrl = MagicMock()
        ctrl.spm = MagicMock()
        ctrl.spm.audit_and_protect_open_positions.return_value = []
        ctrl.get_positions.return_value = []
        ctrl.get_open_orders.return_value = [
            {"symbol": "ETHUSDT", "side": "Buy", "orderType": "Limit", "reduceOnly": False, "orderId": "o-s3", "createdTime": now_ms - 35 * 60_000},
            {"symbol": "SOLUSDT", "side": "Buy", "orderType": "Limit", "reduceOnly": False, "orderId": "o-mom", "createdTime": now_ms - 95 * 60_000},
        ]
        ctrl.ledger.pending_strategy.side_effect = lambda sym, side: "CORE:S3_VWAP_RANGE" if sym == "ETHUSDT" else "CORE:AUTOTRADE_MOMENTUM_FUNDING"
        ctrl.cancel_order.return_value = {"ok": True}

        st._run_guard_cycle_for_controller(ctrl)
        cancelled_ids = {call.args[1] for call in ctrl.cancel_order.call_args_list}
        self.assertEqual(cancelled_ids, {"o-s3", "o-mom"})


if __name__ == "__main__":
    unittest.main()

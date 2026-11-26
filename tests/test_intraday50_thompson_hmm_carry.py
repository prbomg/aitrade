# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import random
import tempfile
import unittest
from pathlib import Path

from aitrade.booster.ml_calibrator import AdaptiveMLCalibrator
from aitrade.exchange.bybit_v5 import Candle
from aitrade.ml.regime_hmm import evaluate_coarse_regime_gate
from aitrade.strategies.funding_carry import FundingCarryEngine
from aitrade.strategies.minvar_portfolio import compute_ledoit_wolf_cov, optimize_long_only_minvar


def _make_candles(prices):
    out = []
    base_ts = 1700000000000
    for i, p in enumerate(prices):
        out.append(
            Candle(
                timestamp=base_ts + i * 3600 * 1000,
                open=p * 0.999,
                high=p * 1.003,
                low=p * 0.997,
                close=p,
                volume=1000.0,
                turnover=p * 1000.0,
            )
        )
    return out


class TestIntraday50ThompsonHmmCarryMinVar(unittest.TestCase):
    def test_thompson_sampling_weights_and_safety_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal = AdaptiveMLCalibrator(Path(tmp))
            closed = []
            ts = 1700000000000
            # T1: 24 сделки, сильный плюс (18 побед, 6 стопов) -> BOOST
            for i in range(24):
                closed.append({
                    "strategy_id": "CORE:T1_DONCHIAN_TSMOM",
                    "closedPnl": 25.0 if i % 4 != 0 else -12.0,
                    "updatedTime": ts + i * 60000,
                })
            # B4: 26 сделок, сильный минус -> QUARANTINE
            for i in range(26):
                closed.append({
                    "strategy_id": "BOOSTER:B4_ORB_SESSION_BREAKOUT",
                    "closedPnl": -20.0 if i % 5 != 0 else 5.0,
                    "updatedTime": ts + (50 + i) * 60000,
                })
            # B1: 5 сделок (< 20) -> строго NORMAL 1.0x
            for i in range(5):
                closed.append({
                    "strategy_id": "BOOSTER:B1_SQUEEZE_BREAKOUT",
                    "closedPnl": -10.0,
                    "updatedTime": ts + (100 + i) * 60000,
                })

            summary = cal.calibrate_from_closed_pnl(closed, force=True)
            by_name = {s["strategy_name"]: s for s in summary["strategy_list"]}

            self.assertEqual(by_name["B1_SQUEEZE_BREAKOUT"]["status"], "NORMAL")
            self.assertEqual(by_name["B1_SQUEEZE_BREAKOUT"]["weight_multiplier"], 1.0)
            self.assertEqual(by_name["B4_ORB_SESSION_BREAKOUT"]["status"], "QUARANTINE")
            self.assertEqual(by_name["B4_ORB_SESSION_BREAKOUT"]["weight_multiplier"], 0.0)
            self.assertEqual(by_name["T1_DONCHIAN_TSMOM"]["status"], "BOOST")
            self.assertGreater(by_name["T1_DONCHIAN_TSMOM"]["thompson_mean"], 0.65)

            sampled = cal.sample_thompson_weights(seed=7)
            self.assertEqual(sampled["B4_ORB_SESSION_BREAKOUT"], 0.0)
            self.assertGreater(sampled["T1_DONCHIAN_TSMOM"], sampled["B1_SQUEEZE_BREAKOUT"])

    def test_coarse_regime_gate_high_vol_and_mean_reversion(self):
        # 1) Режим высокой волатильности (резкие скачки ±4% каждый час)
        prices_hv = [100.0]
        for i in range(1, 140):
            step = 1.045 if i % 2 == 0 else 0.957
            prices_hv.append(prices_hv[-1] * step)
        gate_hv = evaluate_coarse_regime_gate(_make_candles(prices_hv), strategy_id="T1_DONCHIAN_TSMOM")
        self.assertEqual(gate_hv.vol_regime, "HIGH_VOL")
        self.assertAlmostEqual(gate_hv.size_multiplier, 0.65, places=2)
        self.assertFalse(gate_hv.allow_mean_reversion)

        gate_hv_mr = evaluate_coarse_regime_gate(_make_candles(prices_hv), strategy_id="S3_VWAP_RANGE")
        self.assertEqual(gate_hv_mr.size_multiplier, 0.0)

        # 2) Спокойный боковик -> контртренд разрешён
        prices_flat = [100.0 + 0.25 * math.sin(i * 0.35) for i in range(140)]
        gate_flat = evaluate_coarse_regime_gate(_make_candles(prices_flat), strategy_id="S3_VWAP_RANGE")
        self.assertNotEqual(gate_flat.vol_regime, "HIGH_VOL")
        self.assertEqual(gate_flat.regime, "SIDEWAYS")
        self.assertTrue(gate_flat.allow_mean_reversion)
        self.assertEqual(gate_flat.size_multiplier, 1.0)

    def test_funding_carry_tail_risk_protections(self):
        eng = FundingCarryEngine()
        good_fr = [0.00035] * 10

        # Штатный вход + проверка жёсткого потолка плеча <= 2.0x
        opp_ok = eng.evaluate_pair("BTCUSDT", good_fr, 100000.0, 100050.0, leverage=5.0)
        self.assertEqual(opp_ok.action, "OPEN_CARRY")
        self.assertLessEqual(opp_ok.recommended_leverage, 2.0)
        self.assertTrue(any("LEVERAGE_CLAMPED" in f for f in opp_ok.risk_flags))

        # Блок входа при депег-залоге / остановке выводов / высоком ADL
        self.assertEqual(
            eng.evaluate_pair("BTCUSDT", good_fr, 100000.0, 100050.0, collateral_matches_settlement=False).action,
            "SKIP",
        )
        self.assertEqual(
            eng.evaluate_pair("BTCUSDT", good_fr, 100000.0, 100050.0, transfers_enabled=False).action,
            "SKIP",
        )
        self.assertEqual(
            eng.evaluate_pair("BTCUSDT", good_fr, 100000.0, 100050.0, adl_rank=4).action,
            "SKIP",
        )

        # Аварийное закрытие открытой carry-связки при ADL >= 4 или обвале базиса < -0.50%
        self.assertEqual(
            eng.evaluate_pair("ETHUSDT", good_fr, 3000.0, 3001.0, has_open_carry=True, adl_rank=4).action,
            "CLOSE_CARRY",
        )
        self.assertEqual(
            eng.evaluate_pair("ETHUSDT", good_fr, 3000.0, 2970.0, has_open_carry=True).action,
            "CLOSE_CARRY",
        )

    def test_long_only_minvar_portfolio_optimizer(self):
        rng = random.Random(42)
        n_obs = 120
        # BTC — низкая волатильность, ETH — средняя, DOGE/PEPE — высокая
        mkt = [rng.gauss(0.0005, 0.012) for _ in range(n_obs)]
        rets = {
            "BTCUSDT": [0.7 * m + rng.gauss(0.0, 0.005) for m in mkt],
            "ETHUSDT": [1.0 * m + rng.gauss(0.0, 0.010) for m in mkt],
            "SOLUSDT": [1.4 * m + rng.gauss(0.0, 0.022) for m in mkt],
            "DOGEUSDT": [1.8 * m + rng.gauss(0.0, 0.045) for m in mkt],
        }
        cov, delta = compute_ledoit_wolf_cov([[rets[s][t] for s in sorted(rets)] for t in range(n_obs)])
        self.assertEqual(len(cov), 4)
        self.assertGreaterEqual(delta, 0.0)
        self.assertLessEqual(delta, 1.0)

        res = optimize_long_only_minvar(rets, max_weight=0.50, min_obs=30)
        self.assertAlmostEqual(sum(res.weights.values()), 1.0, places=5)
        for w in res.weights.values():
            self.assertGreaterEqual(w, 0.0)
            self.assertLessEqual(w, 0.500001)
        # Минимальная дисперсия должна дать наибольший вес BTC и наименьший DOGE, и снизить волатильность vs 1/N
        self.assertGreater(res.weights["BTCUSDT"], res.weights["DOGEUSDT"])
        self.assertLessEqual(res.portfolio_vol_ann, res.equal_weight_vol_ann)
        self.assertGreater(res.vol_reduction_pct, 5.0)


if __name__ == "__main__":
    unittest.main()

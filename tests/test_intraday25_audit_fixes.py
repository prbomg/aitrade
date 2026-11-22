import unittest
from types import SimpleNamespace

from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.dashboard.live_server import ControlCenterState


class TestIntraday25AuditFixes(unittest.TestCase):
    def test_demo_lab_booster_sizing_uses_15_usd_risk_instead_of_5_usd(self) -> None:
        base_metrics = {
            "isolated_subaccount": False,
            "risk_capital_usd": 1000.0,
            "effective_risk_usd": 5.0,
            "effective_risk_pct": 0.5,
            "recommended_leverage": 3,
            "live_equity": 1000.0,
        }
        demo_metrics = ControlCenterState._demo_lab_booster_sizing_metrics(base_metrics, demo_lab_active=True)
        self.assertEqual(demo_metrics["effective_risk_usd"], 15.0)
        self.assertEqual(demo_metrics["effective_risk_pct"], 1.5)

        # Вне Demo Lab сохраняется консервативный лимит $5 (0.5%)
        prod_metrics = ControlCenterState._demo_lab_booster_sizing_metrics(base_metrics, demo_lab_active=False)
        self.assertEqual(prod_metrics["effective_risk_usd"], 5.0)

        # Проверяем реальный расчёт объёма: при 1% SL + 0.54% буфер издержек, риск $15 даёт ~974 USDT нотионала (3x от $5)
        mgr = DepositBoosterManager.__new__(DepositBoosterManager)
        s_demo = mgr.calculate_order_qty(100.0, 99.0, demo_metrics)
        s_prod = mgr.calculate_order_qty(100.0, 99.0, prod_metrics)
        self.assertAlmostEqual(s_demo["qty_coins"] / s_prod["qty_coins"], 3.0, places=2)
        self.assertEqual(s_demo["risk_usd"], 15.0)

    def test_strategy_priority_tiers_put_wfa_passed_before_combos_and_experimental(self) -> None:
        r = ControlCenterState._strategy_priority_rank
        self.assertLess(r("T1_DONCHIAN_TSMOM"), r("B1_SQUEEZE_BREAKOUT"))
        self.assertLess(r("B1_SQUEEZE_BREAKOUT"), r("C3_ENSEMBLE_VOTE_2OF3"))
        self.assertLess(r("C3_ENSEMBLE_VOTE_2OF3"), r("C2_MTF_COMBO_BREAKOUT"))
        self.assertLess(r("C2_MTF_COMBO_BREAKOUT"), r("B2_DONCHIAN_INTRADAY"))
        self.assertLess(r("B2_DONCHIAN_INTRADAY"), r("B6_EMA_PULLBACK_TREND"))
        self.assertLess(r("B6_EMA_PULLBACK_TREND"), r("S1_TREND_RETEST"))

        # Имитируем конфликт на BTCUSDT: экспериментальная B3 с ML=0.90 не должна обгонять T1 с ML=0.65
        queue = [
            ("BOOSTER", {"strategy_id": "B3_VWAP_MOMENTUM_IMPULSE", "symbol": "BTCUSDT", "ml_win_probability": 0.90}),
            ("CORE", {"strategy_id": "C3_ENSEMBLE_VOTE_2OF3", "symbol": "BTCUSDT", "ml_win_probability": 0.72}),
            ("CORE", {"strategy_id": "T1_DONCHIAN_TSMOM", "symbol": "BTCUSDT", "ml_win_probability": 0.65}),
            ("BOOSTER", {"strategy_id": "B1_SQUEEZE_BREAKOUT", "symbol": "BTCUSDT", "ml_win_probability": 0.68}),
        ]
        queue.sort(key=lambda item: (r(item[1]["strategy_id"]), -float(item[1]["ml_win_probability"])))
        ordered = [item[1]["strategy_id"] for item in queue]
        self.assertEqual(
            ordered,
            ["T1_DONCHIAN_TSMOM", "B1_SQUEEZE_BREAKOUT", "C3_ENSEMBLE_VOTE_2OF3", "B3_VWAP_MOMENTUM_IMPULSE"],
        )

    def test_live_bbo_map_reads_exact_unrounded_bid_ask_from_get_bbo(self) -> None:
        class _FakeWS:
            symbols = ["DOGEUSDT", "BTCUSDT"]

            def get_bbo(self, sym: str, max_age: int = 30):
                if sym == "DOGEUSDT":
                    return (0.14231, 0.14233)
                return (95000.5, 95001.0)

            def get_summary(self):
                raise AssertionError("get_summary не должен вызываться при наличии живого get_bbo")

        dummy = ControlCenterState.__new__(ControlCenterState)
        dummy.ws_hub = _FakeWS()
        dummy.settings = {"universe": {"core_pairs": ["DOGEUSDT", "BTCUSDT"]}}
        bbo = dummy._live_bbo_map()
        self.assertEqual(bbo["DOGEUSDT"], (0.14231, 0.14233))
        self.assertEqual(bbo["BTCUSDT"], (95000.5, 95001.0))


if __name__ == "__main__":
    unittest.main()

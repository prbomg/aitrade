import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.booster.env_manager import BybitEnvManager
from aitrade.booster.oi_squeeze_scanner import OISqueezeScanner
from aitrade.execution.smart_position_manager import SmartPositionManager


class Stage4IsolationAndVaultTests(unittest.TestCase):
    def test_env_manager_subaccount_isolation_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            em = BybitEnvManager(Path(td), default_demo_key="shared_k", default_demo_secret="shared_s")
            # Без отдельных ключей в demo -> fallback на общий ключ (isolated=False)
            b0 = em.get_account_credentials("booster")
            self.assertFalse(b0["isolated"])
            self.assertEqual(b0["api_key"], "shared_k")

            # Сохраняем отдельные ключи субаккаунта booster -> isolated=True
            res = em.save_subaccount_keys("booster", "demo", "boost_k_1234", "boost_s_1234")
            self.assertTrue(res["ok"])
            b1 = em.get_account_credentials("booster")
            a1 = em.get_account_credentials("autotrade")
            self.assertTrue(b1["isolated"])
            self.assertEqual(b1["api_key"], "boost_k_1234")
            self.assertFalse(a1["isolated"])
            self.assertEqual(a1["api_key"], "shared_k")

    def test_deposit_booster_isolated_kelly_damper_and_ratchet_vault(self):
        with tempfile.TemporaryDirectory() as td:
            b = DepositBoosterManager(Path(td), isolated_subaccount=True)
            # 1) На старте $1,000 -> дробный Келли 4.0% = $40
            m0 = b.compute_metrics([], [])
            self.assertTrue(m0["isolated_subaccount"])
            self.assertEqual(m0["base_risk_pct"], 4.0)
            self.assertEqual(m0["effective_risk_usd"], 40.0)
            self.assertEqual(m0["vault_usd"], 0.0)

            # 2) Рост до $1,600 (+60% >= +50% шаг) -> Сейф-храповик фиксирует 35% прибыли ($210)
            b.state["manual_adjustment_usd"] = 600.0
            m1 = b.compute_metrics([], [])
            self.assertAlmostEqual(m1["vault_usd"], 210.0, places=2)
            self.assertAlmostEqual(m1["current_equity"], 1390.0, places=2)
            # Потолок $1,000 снят -> риск считается от $1,390 * 4% = $55.60
            self.assertAlmostEqual(m1["effective_risk_usd"], 55.6, places=2)

            # 3) Просадка >= 20% от active HWM -> демпфер режет риск вдвое (2.0%)
            b.state["active_high_water_mark"] = 2000.0
            b.state["manual_adjustment_usd"] = 400.0  # gross=1400, active=1190 (< 1600, DD > 20%)
            m2 = b.compute_metrics([], [])
            self.assertTrue(m2["near_floor_protection"])
            self.assertEqual(m2["base_risk_pct"], 2.0)

    def test_spm_isolated_margin_and_volatility_trailing(self):
        with tempfile.TemporaryDirectory() as td:
            sp = SmartPositionManager("k", "s")
            sp.guard_path = Path(td) / "guard.json"
            sp.split_tp_enabled = False
            sp.signed_post = Mock(return_value={"retCode": 0, "retMsg": "OK", "result": {}})

            iso = sp.ensure_isolated_margin("BTCUSDT", leverage=4)
            self.assertTrue(iso["ok"])
            self.assertEqual(sp.signed_post.call_args.args[0], "/v5/position/switch-isolated")
            self.assertEqual(sp.signed_post.call_args.args[1]["tradeMode"], 1)

            # 1) B1_SQUEEZE_BREAKOUT работает по чистому SL/TP (без раннего БУ и трейлинга)
            sp.ledger = NS(
                active_intent=lambda *a: {
                    "intent_id": "b1-entry",
                    "strategy_id": "BOOSTER:B1_SQUEEZE_BREAKOUT",
                    "sl": 98.0,
                    "signal_context": {"features": {"atr15": 1.2}},
                }
            )
            pos = {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "positionIdx": 0,
                "size": "10",
                "avgPrice": "100",
                "markPrice": "103.8",
                "stopLoss": "98",
                "takeProfit": "107",
                "trailingStop": "0",
            }
            sp.paged_get = lambda path, p: [pos] if path.endswith("/position/list") else []
            sp.move_sl_to_breakeven = Mock(return_value={"ok": True})
            sp.activate_trailing_stop = Mock(return_value={"ok": True})

            sp.audit_and_protect_open_positions()
            sp.move_sl_to_breakeven.assert_not_called()
            sp.activate_trailing_stop.assert_not_called()

            # 2) Прочие BOOSTER:MANUAL сохраняют волатильностный трейлинг 1.8 * ATR15 после +1.8R
            sp.ledger = NS(
                active_intent=lambda *a: {
                    "intent_id": "b-manual",
                    "strategy_id": "BOOSTER:MANUAL",
                    "sl": 98.0,
                    "signal_context": {"features": {"atr15": 1.2}},
                }
            )
            sp.audit_and_protect_open_positions()
            sp.move_sl_to_breakeven.assert_called_once()
            sp.activate_trailing_stop.assert_called_once()
            trail_pct_used = sp.activate_trailing_stop.call_args.args[2]
            self.assertGreaterEqual(trail_pct_used, 2.15)

    def test_oi_scanner_uses_closed_candles_and_no_autotrade(self):
        sc = OISqueezeScanner()
        # k_list[0] — гигантская незакрытая свеча, которая НЕ должна влиять на ATR
        fake_klines = [
            ["1700000900000", "100", "200", "50", "150", "9999", "999999"],  # незакрытая!
        ] + [
            [str(1700000000000 - i * 900000), "100", "100.8", "99.2", "100.1", "100", "10000"]
            for i in range(16)
        ]
        sc._curl_json = Mock(return_value={"retCode": 0, "result": {"list": fake_klines}})
        atr_pct, ob, os_, note = sc._compute_15m_atr_and_anti_chase("BTCUSDT", 100.1)
        self.assertLess(atr_pct, 2.0)  # Если бы учли k_list[0], ATR был бы у потолка 2.5%
        self.assertIn("closed", note)


if __name__ == "__main__":
    unittest.main()

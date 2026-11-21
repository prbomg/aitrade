import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import Candle
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.strategies.base import TradeSignal
from aitrade.strategies.donchian_intraday import generate_donchian_intraday_signal
from aitrade.strategies.vwap_momentum_impulse import generate_vwap_momentum_signal


class TestIntraday20DemoLab(unittest.TestCase):
    def test_b2_donchian_intraday_generates_breakout_signal_in_uptrend(self) -> None:
        ts0 = 1_760_000_000_000
        # 60 баров 4h в уверенном аптренде (EMA20 > EMA50)
        bars_4h = [
            Candle(ts0 + i * 14_400_000, 100.0 + i * 0.5, 101.0 + i * 0.5, 99.5 + i * 0.5, 100.5 + i * 0.5, 1000.0, 100_000.0)
            for i in range(60)
        ]
        # 40 баров 1h в боковике 128..130 и последний бар пробивает вверх до 133.5
        bars_1h = [
            Candle(ts0 + i * 3_600_000, 129.0, 130.0, 128.0, 129.2, 1000.0, 100_000.0)
            for i in range(39)
        ]
        bars_1h.append(Candle(ts0 + 39 * 3_600_000, 129.5, 134.0, 129.2, 133.5, 2500.0, 300_000.0))

        sig = generate_donchian_intraday_signal("SOLUSDT", bars_1h, bars_4h=bars_4h)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B2_DONCHIAN_INTRADAY")
        self.assertEqual(sig.side, "Buy")
        self.assertGreater(sig.take_profit_2, sig.entry_price)
        self.assertLess(sig.stop_loss, sig.entry_price)

    def test_b3_vwap_momentum_impulse_generates_signal_on_volume_surge(self) -> None:
        # Все свечи внутри одних UTC-суток
        ts0 = 1_760_054_400_000  # 00:00 UTC
        bars_1h = [
            Candle(ts0 + i * 3_600_000, 100.0 + i * 0.4, 101.0 + i * 0.4, 99.5 + i * 0.4, 100.4 + i * 0.4, 1000.0, 100_000.0)
            for i in range(55)
        ]
        # 40 баров 15m вокруг 100..100.2, затем импульс вверх до 100.65 (Z ~ +1.5σ)
        bars_15m = [
            Candle(ts0 + i * 900_000, 100.0, 100.6, 99.4, 100.1 + (i % 3) * 0.05, 1000.0, 100_000.0)
            for i in range(39)
        ]
        bars_15m.append(Candle(ts0 + 39 * 900_000, 100.15, 100.75, 100.1, 100.65, 3000.0, 300_000.0))

        sig = generate_vwap_momentum_signal("ETHUSDT", bars_15m, bars_1h=bars_1h, rvol=1.8)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B3_VWAP_MOMENTUM_IMPULSE")
        self.assertEqual(sig.side, "Buy")

    def test_demo_lab_admits_experimental_only_on_demo_and_stays_fail_closed_on_mainnet(self) -> None:
        orig_path = WalkForwardMetaLabeler.WFA_VALIDATION_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                WalkForwardMetaLabeler.WFA_VALIDATION_PATH = Path(td) / "missing_wfa.json"
                ts0 = 1_760_000_000_000
                c15 = [Candle(ts0 + i * 900_000, 100.0, 100.5, 99.5, 100.2, 1000.0, 100_000.0) for i in range(60)]
                ctx = NS(
                    candles_15m=c15,
                    micro=NS(smoothed_obi=0.1, vamp_divergence_bps=1.0, liquidity_behavior_index=0.9),
                    screener=NS(rvol=1.6, spread_pct=0.02),
                )
                sig = TradeSignal(
                    symbol="SOLUSDT",
                    strategy_id="B2_DONCHIAN_INTRADAY",
                    strategy_name="Donchian 1h",
                    timeframe="60",
                    side="Buy",
                    order_type="Limit",
                    entry_price=100.0,
                    stop_loss=98.0,
                    take_profit_1=103.0,
                    take_profit_2=106.0,
                    base_winrate_est=0.45,
                    rationale=["test"],
                    features={"post_only": 1.0},
                )

                # 1) На Demo в Demo Lab — допускается с явной пометкой DEMO_LAB_EXPERIMENTAL
                demo_cfg = load_settings()
                demo_cfg["exchange"]["mode"] = "demo"
                demo_cfg["demo_lab"] = {
                    "enabled": True,
                    "admit_experimental_strategies": ["B2_DONCHIAN_INTRADAY"],
                }
                res_demo = WalkForwardMetaLabeler(demo_cfg).evaluate_signal(sig, ctx)
                self.assertTrue(res_demo.passed_ml_filter)
                self.assertEqual(res_demo.features.get("ml_calibration_status"), "DEMO_LAB_EXPERIMENTAL")

                # 2) На Mainnet — жёсткий fail-closed (WFA_VALIDATION_UNAVAILABLE)
                main_cfg = load_settings()
                main_cfg["exchange"]["mode"] = "live"
                main_cfg["demo_lab"] = {
                    "enabled": True,
                    "admit_experimental_strategies": ["B2_DONCHIAN_INTRADAY"],
                }
                res_main = WalkForwardMetaLabeler(main_cfg).evaluate_signal(sig, ctx)
                self.assertFalse(res_main.passed_ml_filter)
                self.assertEqual(res_main.features.get("ml_calibration_status"), "WFA_VALIDATION_UNAVAILABLE")
        finally:
            WalkForwardMetaLabeler.WFA_VALIDATION_PATH = orig_path


if __name__ == "__main__":
    unittest.main()

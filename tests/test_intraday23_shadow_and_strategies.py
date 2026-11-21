import tempfile
import unittest
from pathlib import Path

from aitrade.exchange.bybit_v5 import Candle
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.strategies.ema_pullback_trend import generate_ema_pullback_signal
from aitrade.strategies.funding_squeeze_momentum import generate_funding_squeeze_signal
from aitrade.strategies.keltner_trend_4h import generate_keltner_trend_4h_signal


class TestIntraday23ShadowAndStrategies(unittest.TestCase):
    def test_b6_ema_pullback_trend_fires_on_pullback_in_uptrend(self) -> None:
        ts0 = 1_760_000_000_000
        # 4h аптренд
        bars_4h = [Candle(ts0 + i * 14_400_000, 90.0 + i * 0.4, 91.0 + i * 0.4, 89.5 + i * 0.4, 90.6 + i * 0.4, 1000.0, 100_000.0) for i in range(60)]
        # 1h направленный тренд (ADX >= 20)
        bars_1h = [Candle(ts0 + i * 3_600_000, 100.0 + i * 0.35, 100.8 + i * 0.35, 99.8 + i * 0.35, 100.4 + i * 0.35, 1000.0, 100_000.0) for i in range(35)]
        # 15m: рост, потом лёгкий откат вниз к EMA21, затем бычий отскок
        bars_15m = []
        px = 110.0
        for i in range(35):
            px += 0.12 if i < 28 else -0.25
            bars_15m.append(Candle(ts0 + i * 900_000, px - 0.05, px + 0.25, px - 0.25, px, 1000.0, 100_000.0))
        # Предпоследняя свеча касается EMA21, последняя закрывается выше с ростом RSI
        for i in range(35, 39):
            px -= 0.15
            bars_15m.append(Candle(ts0 + i * 900_000, px + 0.1, px + 0.2, px - 0.3, px, 1000.0, 100_000.0))
        bars_15m.append(Candle(ts0 + 39 * 900_000, px - 0.05, px + 1.4, px - 0.4, px + 1.2, 2000.0, 220_000.0))

        sig = generate_ema_pullback_signal("SOLUSDT", bars_15m, bars_1h, bars_4h)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B6_EMA_PULLBACK_TREND")
        self.assertEqual(sig.side, "Buy")

    def test_b7_funding_squeeze_momentum_fires_on_negative_funding_breakout(self) -> None:
        ts0 = 1_760_000_000_000
        bars_1h = [Candle(ts0 + i * 3_600_000, 100.0, 100.8, 99.2, 100.1, 1000.0, 100_000.0) for i in range(24)]
        bars_1h.append(Candle(ts0 + 24 * 3_600_000, 100.2, 102.0, 100.0, 101.8, 3000.0, 300_000.0))
        sig = generate_funding_squeeze_signal("SUIUSDT", bars_1h, funding_rate=-0.0003)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B7_FUNDING_SQUEEZE_MOMENTUM")
        self.assertEqual(sig.side, "Buy")

    def test_t3_keltner_trend_4h_fires_on_fresh_channel_break(self) -> None:
        ts0 = 1_760_000_000_000
        bars_4h = [Candle(ts0 + i * 14_400_000, 100.0 + i * 0.25, 100.8 + i * 0.25, 99.6 + i * 0.25, 100.2 + i * 0.25, 1000.0, 100_000.0) for i in range(38)]
        # Резкий пробой выше EMA20 + 2.2*ATR
        last_base = 100.0 + 38 * 0.25
        bars_4h.append(Candle(ts0 + 38 * 14_400_000, last_base, last_base + 5.5, last_base - 0.3, last_base + 5.2, 4000.0, 450_000.0))
        sig = generate_keltner_trend_4h_signal("BTCUSDT", bars_4h)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "T3_KELTNER_TREND_4H")
        self.assertEqual(sig.side, "Buy")

    def test_shadow_engine_runs_multiple_strategies_on_same_symbol_without_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng = LiveShadowEngine(Path(td) / "shadow.sqlite3")
            t0 = 1_800_000_000.0
            signals = [
                {"strategy_id": "B2_DONCHIAN_INTRADAY", "symbol": "SOLUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 106.0},
                {"strategy_id": "B6_EMA_PULLBACK_TREND", "symbol": "SOLUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 106.0},
            ]
            # Обе стратегии открывают SOLUSDT одновременно без конфликта!
            opened = eng.record_signals(signals, {"SOLUSDT": (99.98, 100.02)}, now_ts=t0)
            self.assertEqual(opened, 2)

            # Цена доходит до TP 106.0 -> обе закрываются в плюс по TP
            closed = eng.update_open_positions({"SOLUSDT": (106.10, 106.14)}, now_ts=t0 + 7200)
            self.assertEqual(closed, 2)

            summary = eng.get_summary()
            self.assertEqual(summary["total_open"], 0)
            self.assertEqual(summary["total_closed"], 2)
            self.assertEqual(len(summary["leaderboard"]), 2)
            for row in summary["leaderboard"]:
                self.assertGreater(row["net_pnl_usd"], 35.0)  # ~+3R ($45) минус комиссии
                self.assertGreater(row["pod_equity_usd"], 1035.0)
                self.assertEqual(row["win_rate_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()

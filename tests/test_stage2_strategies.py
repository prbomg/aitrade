import unittest

from aitrade.exchange.bybit_v5 import Candle
from aitrade.strategies.booster_breakout import generate_booster_breakout_signal
from aitrade.strategies.funding_carry import FundingCarryEngine
from aitrade.strategies.momentum_funding import (
    generate_momentum_funding_signals,
    rank_momentum_funding,
)
from aitrade.strategies.regime_s1_s3 import generate_s1_s3_signal


def _make_1h_trend(n: int = 120, direction: int = 1, start_ms: int = 1_700_000_000_000) -> list[Candle]:
    bars = []
    px = 100.0
    for i in range(n):
        step = 0.0055 * direction if i >= 40 else 0.001 * direction
        nxt = px * (1.0 + step)
        rng = abs(nxt - px) + (0.9 if i >= 70 else 0.25)
        bars.append(Candle(start_ms + i * 3_600_000, px, max(px, nxt) + rng * 0.4, min(px, nxt) - rng * 0.4, nxt, 1000.0, 1000.0 * nxt))
        px = nxt
    return bars


def _make_1h_range(n: int = 120, start_ms: int = 1_700_000_000_000) -> list[Candle]:
    bars = []
    for i in range(n):
        amp = 2.5 if i < 60 else 0.25
        c_px = 100.0 + (amp if i % 2 == 0 else -amp)
        bars.append(Candle(start_ms + i * 3_600_000, 100.0, max(100.0, c_px) + amp * 0.2, min(100.0, c_px) - amp * 0.2, c_px, 1000.0, 1000.0 * c_px))
    return bars


class Stage2StrategiesTests(unittest.TestCase):
    def test_s1_passive_post_only_retest_below_market(self):
        h1 = _make_1h_trend(120, direction=1)
        start_ms = 1_700_000_000_000
        bars_15m = []
        for i in range(60):
            px = 100.0 + (0.05 if i % 2 == 0 else -0.05)
            bars_15m.append(Candle(start_ms + i * 900_000, px, px + 0.2, px - 0.2, px, 100.0, 10000.0))
        # Пробойная свеча вверх с объёмом
        bars_15m.append(Candle(start_ms + 60 * 900_000, 100.1, 101.8, 100.0, 101.5, 250.0, 25000.0))

        sig = generate_s1_s3_signal(
            "BTCUSDT",
            bars_15m,
            reference_open_px=101.5,
            bars_1h=h1,
            best_bid=101.4,
            best_ask=101.5,
            tick_size=0.1,
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "S1_TREND_RETEST")
        self.assertEqual(sig.order_type, "Limit")
        self.assertEqual(sig.features.get("post_only"), 1.0)
        # Лимитка на покупку строго ниже best_bid (никакого Taker-пробоя!)
        self.assertLessEqual(sig.entry_price, 101.3)
        # Стоп не уже 1.0% для мейджора
        self.assertGreaterEqual((sig.entry_price - sig.stop_loss) / sig.entry_price, 0.010 - 1e-6)

    def test_s3_requires_range_post_only_and_rr_115(self):
        h1_rng = _make_1h_range(120)
        # Сессия с 00:00 UTC
        day0 = 1_700_006_400_000
        bars_15m = []
        for i in range(60):
            # Узкий флэт вокруг 100.0 (ATR ~0.35), чтобы отбой от -2sd давал R:R >= 1.15
            c = 100.0 + (0.25 if i % 2 == 0 else -0.25)
            bars_15m.append(Candle(day0 + i * 900_000, 100.0, max(100.0, c) + 0.08, min(100.0, c) - 0.08, c, 200.0, 20000.0))
        # Прокол вниз к 98.3 (далеко за -2sd) с бычьим отбоем (расстояние до VWAP ~1.6%, SL ~0.8% -> R:R ~2.0)
        bars_15m.append(Candle(day0 + 60 * 900_000, 98.2, 98.55, 98.1, 98.4, 300.0, 29500.0))

        sig = generate_s1_s3_signal(
            "ETHUSDT",
            bars_15m,
            reference_open_px=98.4,
            bars_1h=h1_rng,
            best_bid=98.38,
            best_ask=98.40,
            tick_size=0.01,
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "S3_VWAP_RANGE")
        self.assertEqual(sig.order_type, "Limit")
        self.assertEqual(sig.features.get("post_only"), 1.0)
        risk = sig.entry_price - sig.stop_loss
        reward = sig.take_profit_2 - sig.entry_price
        self.assertGreaterEqual(risk / sig.entry_price, 0.0080 - 1e-6)
        self.assertGreaterEqual(reward / risk, 1.15 - 1e-6)

    def test_b1_squeeze_breakout_and_funding_filter(self):
        h1_up = _make_1h_trend(120, direction=1)
        start_ms = 1_700_000_000_000
        bars_15m = []
        # Сначала широкий диапазон (ATR высокий), затем узкое сжатие BB внутри Keltner
        for i in range(65):
            if i < 35:
                o, h, l, c = 100.0, 102.0, 98.0, 100.0 + (0.8 if i % 2 == 0 else -0.8)
            else:
                o, h, l, c = 100.0, 100.12, 99.88, 100.0 + (0.04 if i % 2 == 0 else -0.04)
            bars_15m.append(Candle(start_ms + i * 900_000, o, h, l, c, 100.0, 10000.0))
        # Пробойная свеча вверх с RVOL >= 1.35
        bars_15m.append(Candle(start_ms + 65 * 900_000, 100.05, 103.5, 100.0, 103.2, 300.0, 30960.0))

        # С нормальным фандингом сигнал проходит
        sig = generate_booster_breakout_signal("AVAXUSDT", bars_15m, reference_open_px=103.2, bars_1h=h1_up, funding_rate=0.0001)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.strategy_id, "B1_SQUEEZE_BREAKOUT")
        self.assertEqual(sig.side, "Buy")
        self.assertGreaterEqual((sig.entry_price - sig.stop_loss) / sig.entry_price, 0.015 - 1e-6)

        # С перегретым фандингом > +0.03% лонг блокируется
        blocked = generate_booster_breakout_signal("AVAXUSDT", bars_15m, reference_open_px=103.2, bars_1h=h1_up, funding_rate=0.00045)
        self.assertIsNone(blocked)

    def test_momentum_funding_cross_sectional_selection(self):
        strong = _make_1h_trend(40, direction=1)
        weak = _make_1h_trend(40, direction=-1)
        flat = _make_1h_range(40)
        data = {"SOLUSDT": strong, "AVAXUSDT": weak, "BTCUSDT": flat}
        fr = {"SOLUSDT": -0.0001, "AVAXUSDT": 0.0004, "BTCUSDT": 0.0001}

        ranked = rank_momentum_funding(data, fr)
        self.assertEqual(ranked[0].symbol, "SOLUSDT")
        self.assertEqual(ranked[-1].symbol, "AVAXUSDT")

        sigs = generate_momentum_funding_signals(data, fr, top_n=1, min_mom_threshold=0.5)
        self.assertEqual(len(sigs), 2)
        by_side = {s.side: s for s in sigs}
        self.assertEqual(by_side["Buy"].symbol, "SOLUSDT")
        self.assertEqual(by_side["Sell"].symbol, "AVAXUSDT")

    def test_funding_carry_open_hold_and_close(self):
        eng = FundingCarryEngine()
        # 9 интервалов устойчивого фандинга 0.035%/8h -> OPEN_CARRY
        op = eng.evaluate_pair("BTCUSDT", [0.00035] * 9, spot_price=100.0, perp_price=100.05, has_open_carry=False)
        self.assertEqual(op.action, "OPEN_CARRY")
        self.assertGreater(op.annualized_apr_pct, 30.0)

        # Падение фандинга ниже 0.005%/8h -> CLOSE_CARRY
        cl = eng.evaluate_pair("BTCUSDT", [0.00035] * 6 + [0.00002, 0.00001, 0.00001], spot_price=100.0, perp_price=100.01, has_open_carry=True)
        self.assertEqual(cl.action, "CLOSE_CARRY")


if __name__ == "__main__":
    unittest.main()

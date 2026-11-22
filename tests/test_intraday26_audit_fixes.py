import tempfile
import time
import unittest
from pathlib import Path

from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard
from aitrade.exchange.bybit_v5 import OrderBookLevel, OrderBookSnapshot
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.risk.risk_engine import RiskEngine
from aitrade.strategies.base import TradeSignal


class TestIntraday26AuditFixes(unittest.TestCase):
    def test_risk_engine_uses_15_usd_risk_for_core_strategies_in_demo_lab(self) -> None:
        ob = OrderBookSnapshot(
            symbol="SOLUSDT",
            bids=[OrderBookLevel(99.99, 5000.0)],
            asks=[OrderBookLevel(100.01, 5000.0)],
            timestamp=1700000000000,
        )
        sig_demo = TradeSignal(
            symbol="SOLUSDT",
            strategy_id="T3_KELTNER_TREND_4H",
            strategy_name="Keltner 4h",
            timeframe="240",
            side="Buy",
            order_type="Market",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit_1=106.0,
            take_profit_2=106.0,
            base_winrate_est=0.52,
        )
        sig_prod = TradeSignal(
            symbol="SOLUSDT",
            strategy_id="T3_KELTNER_TREND_4H",
            strategy_name="Keltner 4h",
            timeframe="240",
            side="Buy",
            order_type="Market",
            entry_price=100.0,
            stop_loss=98.0,
            take_profit_1=106.0,
            take_profit_2=106.0,
            base_winrate_est=0.52,
        )
        re_demo = RiskEngine({"exchange": {"mode": "demo"}, "risk": {"risk_per_trade_pct": 0.0075}, "demo_lab": {"enabled": True}})
        re_prod = RiskEngine({"exchange": {"mode": "demo"}, "risk": {"risk_per_trade_pct": 0.0075}, "demo_lab": {"enabled": False}})
        re_demo.size_and_validate_signal(sig_demo, ob, 1000.0)
        re_prod.size_and_validate_signal(sig_prod, ob, 1000.0)
        # В Demo Lab риск 1.5% ($15), вне Demo Lab — 0.75% ($7.50) -> объём ровно в 2 раза больше
        self.assertAlmostEqual(sig_demo.position_qty / sig_prod.position_qty, 2.0, places=2)

    def test_btc_regime_guard_uses_fresh_cache_on_transient_error_and_exempts_t3(self) -> None:
        guard = RegimeAndCorrelationGuard()
        guard.last_regime = {
            "state": "DUMP_GUARD",
            "label": "BTC ПРОЛИВ",
            "btc_price": 90000.0,
            "allow_long": False,
            "allow_short": True,
            "reason": "test dump",
        }
        guard._last_ok_ts = time.time() - 20.0
        guard._last_fetch_ts = 0.0
        guard._curl_json = lambda url: (_ for _ in ()).throw(RuntimeError("transient timeout"))  # type: ignore

        reg = guard.update_btc_regime({"metrics": []})
        self.assertEqual(reg["state"], "DUMP_GUARD")

        # T3_KELTNER_TREND_4H не должен блокироваться 5-минутным BTC Guard
        ok_t3, _ = guard.can_open_position("SOLUSDT", "Buy", [], {"metrics": []}, strategy_id="CORE:T3_KELTNER_TREND_4H")
        self.assertTrue(ok_t3)

        # Обычная 15m-стратегия при DUMP_GUARD блокируется на лонг
        ok_b3, _ = guard.can_open_position("SOLUSDT", "Buy", [], {"metrics": []}, strategy_id="BOOSTER:B3_VWAP_MOMENTUM_IMPULSE")
        self.assertFalse(ok_b3)

    def test_shadow_engine_profit_factor_is_none_when_zero_losses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            eng = LiveShadowEngine(Path(td) / "shadow.sqlite3")
            t0 = 1_800_000_000.0
            eng.record_signals(
                [{"strategy_id": "T3_KELTNER_TREND_4H", "symbol": "BTCUSDT", "side": "Buy", "entry_price": 100.0, "stop_loss": 98.0, "take_profit_2": 106.0}],
                {"BTCUSDT": (99.99, 100.01)},
                now_ts=t0,
            )
            eng.update_open_positions({"BTCUSDT": (106.2, 106.3)}, now_ts=t0 + 3600)
            row = eng.get_summary()["leaderboard"][0]
            self.assertIsNone(row["profit_factor"])
            self.assertGreater(row["net_pnl_usd"], 35.0)


if __name__ == "__main__":
    unittest.main()

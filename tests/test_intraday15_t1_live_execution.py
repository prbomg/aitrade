# -*- coding: utf-8 -*-
import unittest

from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard


class TestIntraday15T1LiveExecution(unittest.TestCase):
    def test_regime_guard_allows_t1_4h_up_to_4_positions_and_skips_5m_btc_veto(self):
        guard = RegimeAndCorrelationGuard()
        # Имитируем 5m пролив BTC (DUMP_GUARD)
        guard.last_regime = {
            "state": "DUMP_GUARD",
            "label": "BTC DUMP",
            "btc_price": 60000.0,
            "allow_long": False,
            "allow_short": True,
            "reason": "5m dump",
        }
        guard._last_fetch_ts = 10**12  # заморозить сетевой запрос

        # 15m стратегия должна быть заблокирована 5m проливом BTC
        ok_b1, reason_b1 = guard.can_open_position("ETHUSDT", "Buy", [], {}, strategy_id="BOOSTER:B1_SQUEEZE_BREAKOUT")
        self.assertFalse(ok_b1)
        self.assertIn("BTC Guard", reason_b1)

        # 4h T1_DONCHIAN_TSMOM игнорирует 5m BTC шум и допускает до 2 позиций в секторе и 4 всего
        existing_3 = [
            {"symbol": "BTCUSDT", "side": "Buy"},   # MAJORS #1
            {"symbol": "SUIUSDT", "side": "Buy"},   # L1_L2 #1
            {"symbol": "LINKUSDT", "side": "Buy"},  # DEFI_RWA #1
        ]
        ok_t1, _ = guard.can_open_position("ETHUSDT", "Buy", existing_3, {}, strategy_id="CORE:T1_DONCHIAN_TSMOM")
        self.assertTrue(ok_t1)

        # Но 5-я позиция в ту же сторону (при лимите 4) должна блокироваться
        existing_4 = existing_3 + [{"symbol": "ETHUSDT", "side": "Buy"}]
        ok_t1_5, _ = guard.can_open_position("DOGEUSDT", "Buy", existing_4, {}, strategy_id="CORE:T1_DONCHIAN_TSMOM")
        self.assertFalse(ok_t1_5)

        # Дубликат символа всё равно блокируется
        ok_dup, _ = guard.can_open_position("BTCUSDT", "Buy", existing_3, {}, strategy_id="CORE:T1_DONCHIAN_TSMOM")
        self.assertFalse(ok_dup)


if __name__ == "__main__":
    unittest.main()

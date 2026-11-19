import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.dashboard.live_server import BybitV5DirectController, ControlCenterState
from aitrade.engine import is_in_autotrade_liquid_window
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.storage import Ledger


class P0P1WiringIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_autotrade_liquid_window_filter(self):
        cfg = {"sessions_utc": {"autotrade_liquid_window": {"start_hour": 7, "end_hour": 20}}}
        # 2026-11-19 12:00 UTC -> внутри окна
        noon_ms = 1795089600000
        # 2026-11-19 03:00 UTC -> вне окна (ночная Азия)
        night_ms = noon_ms - 9 * 3600 * 1000
        self.assertTrue(is_in_autotrade_liquid_window(noon_ms, cfg))
        self.assertFalse(is_in_autotrade_liquid_window(night_ms, cfg))

    def test_place_checked_isolated_booster_allows_5x_and_4pct_kelly_and_blocks_low_rr_s3(self):
        c = BybitV5DirectController.__new__(BybitV5DirectController)
        c.base_url = "https://api-demo.bybit.com"
        c._entry_lock = threading.RLock()
        c.engine = NS(settings={"universe": {"core_pairs": ["BTCUSDT"]}})
        c.ledger = Ledger(self.path / "booster_ledger.db")

        booster = DepositBoosterManager(self.path, isolated_subaccount=True)
        booster.state["manual_adjustment_usd"] = 1000.0  # $2,000 balance (above old $1,000 cap)

        c.owner = NS(
            entry_paused=threading.Event(),
            started_at=time.time() - 300,
            booster=booster,
            ws_hub=NS(get_bbo=lambda *a: (100.0, 100.01), get_summary=lambda: {"flow_window_complete": True, "symbols_live": 1}),
            regime_guard=NS(can_open_position=lambda *a: (True, "ok")),
            funding_rates={"BTCUSDT": 0.0},
            funding_updated_at=time.time(),
        )
        c.get_positions = lambda: []
        c.get_open_orders = lambda: []
        c.get_account_summary = lambda: {"usdt_balance": 2000.0}
        c.get_closed_pnl = lambda: []
        spec = {"qty_step": 0.1, "min_qty": 0.1, "max_qty": 1000.0, "min_notional": 5.0, "tick_size": 0.01}
        c.spm = NS(
            get_spec=lambda _: spec,
            fmt_step=SmartPositionManager.fmt_step,
            ensure_isolated_margin=Mock(return_value={"ok": True}),
            signed_post=Mock(return_value={"retCode": 0, "result": {"orderId": "ord-b1"}}),
            paged_get=lambda *a: [{"positionIdx": 0}],
        )
        c.chaser = NS(enabled=False)

        # 1) Изолированный субаккаунт Разгона: плечо 5x, PostOnly Limit, риск > $5 (снят старый потолок $1k / 3x)
        r_b1 = c.place_order(
            "BTCUSDT",
            "Buy",
            "Limit",
            50.0,
            price=99.9,
            take_profit=105.0,
            stop_loss=98.0,
            leverage=5,
            enable_split_tp=False,
            strategy_id="BOOSTER:B1_SQUEEZE_BREAKOUT",
            signal_context={"features": {"post_only": 1.0}, "cal_weight": 1.0},
        )
        self.assertTrue(r_b1["ok"], r_b1.get("retMsg"))
        c.spm.ensure_isolated_margin.assert_called_once_with("BTCUSDT", 5)
        order_payload = c.spm.signed_post.call_args.args[1]
        self.assertEqual(order_payload["timeInForce"], "PostOnly")
        # Размер позиции соответствует риску ~4% от капитала (>> $5)
        self.assertGreater(float(order_payload["qty"]) * 1.9, 25.0)

        # 2) S3_VWAP_RANGE с R:R = 0.80 (< 1.15) должен жёстко отклоняться
        c.ledger.finish(order_payload["orderLinkId"], "FILLED", {})
        r_s3_bad = c.place_order(
            "BTCUSDT",
            "Buy",
            "Limit",
            5.0,
            price=99.9,
            take_profit=100.7,  # +0.8
            stop_loss=98.9,  # -1.0 -> R:R = 0.80 < 1.15
            leverage=3,
            enable_split_tp=False,
            strategy_id="CORE:S3_VWAP_RANGE",
        )
        self.assertFalse(r_s3_bad["ok"])
        self.assertIn("1.15", r_s3_bad["retMsg"])

    def test_dead_mans_switch_cancels_only_entry_orders_on_ws_loss(self):
        s = ControlCenterState.__new__(ControlCenterState)
        s.started_at = time.time() - 120.0
        s.ws_hub = NS(get_summary=lambda: {"connected": False, "last_msg_age_sec": 45.0})
        open_orders = [
            {"symbol": "BTCUSDT", "orderId": "entry-1", "reduceOnly": False},
            {"symbol": "ETHUSDT", "orderId": "tp-1", "reduceOnly": True},
        ]
        s.bybit = NS(
            get_open_orders=lambda: open_orders,
            cancel_order=Mock(return_value={"ok": True}),
        )
        s.add_log = Mock()

        cancelled = s._check_dead_mans_switch()
        self.assertEqual(cancelled, 1)
        s.bybit.cancel_order.assert_called_once_with("BTCUSDT", "entry-1")


if __name__ == "__main__":
    unittest.main()

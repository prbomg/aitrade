# -*- coding: utf-8 -*-
"""Тесты релиза 2026.11.24-intraday.42: аудит WS, transport, SPM, Chaser, Guard, News и стратегий."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard
from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.exchange import transport
from aitrade.exchange.bybit_v5 import Candle
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.execution.limit_chaser import SmartLimitChaser
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.strategies.donchian_intraday import generate_donchian_intraday_signal


class _FakeSock:
    def __init__(self, chunks: list[bytes]) -> None:
        self._buf = bytearray(b"".join(chunks))
        self.sent = bytearray()

    def recv(self, n: int) -> bytes:
        if not self._buf:
            return b""
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


class TestIntraday42Audit2ResilienceFixes(unittest.TestCase):
    def test_ws_responds_pong_to_server_ping_and_prunes_deque_in_place(self) -> None:
        hub = BybitV5WebSocketHub(["BTCUSDT"])
        # RFC-6455 unmasked server ping frame: FIN=1, opcode=0x9, payload=b"hi"
        ping_frame = bytes([0x89, 0x02]) + b"hi"
        sock = _FakeSock([ping_frame])
        res = hub._read_ws_frame(sock)
        self.assertIsNone(res)
        self.assertGreater(len(sock.sent), 0)
        # First byte of Pong frame must be FIN=1 | opcode=0xA = 0x8A
        self.assertEqual(sock.sent[0], 0x8A)

        # JSON pong updates last_msg_ts
        hub.last_msg_ts = 0.0
        hub._handle_message(json.dumps({"op": "pong"}))
        self.assertGreater(hub.last_msg_ts, 0.0)

        # O(k) _prune_deque
        q = deque([(100.0, 1.0, True), (150.0, 2.0, False), (200.0, 3.0, True)])
        hub._prune_deque(q, 140.0)
        self.assertEqual(len(q), 2)
        self.assertEqual(q[0][0], 150.0)

    def test_transport_retries_get_on_rate_limit_10006_but_never_retries_post(self) -> None:
        calls = {"n": 0}

        class _Resp:
            def __init__(self, payload: dict) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=8):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp({"retCode": 10006, "retMsg": "Too many visits"})
            return _Resp({"retCode": 0, "retMsg": "OK", "result": {"ok": True}})

        with patch("aitrade.exchange.transport.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("aitrade.exchange.transport.time.sleep"):
            out = transport.request("https://api-demo.bybit.com", "k", "s", "GET", "/v5/position/list", {})
            self.assertEqual(out["retCode"], 0)
            self.assertEqual(calls["n"], 2)

        calls["n"] = 0

        def fake_post_boom(req, timeout=8):
            calls["n"] += 1
            raise RuntimeError("boom")

        with patch("aitrade.exchange.transport.urllib.request.urlopen", side_effect=fake_post_boom), \
             patch("aitrade.exchange.transport.time.sleep"):
            with self.assertRaises(transport.ExchangeError) as ctx:
                transport.request("https://api-demo.bybit.com", "k", "s", "POST", "/v5/order/create", {"a": 1})
            self.assertTrue(ctx.exception.uncertain)
            self.assertEqual(calls["n"], 1)

    def test_spm_handles_corrupt_guard_json_and_4h_stop_without_false_liq_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            g_path = Path(td) / "position_guard.json"
            g_path.write_text("{corrupt json", encoding="utf-8")

            spm = SmartPositionManager("k", "s", "https://api-demo.bybit.com", guard_path=g_path)
            spm.ledger = MagicMock()
            spm.ledger.active_intent.return_value = {
                "intent_id": "at-t3",
                "strategy_id": "CORE:T3_KELTNER_TREND_4H",
                "sl": 93.0,  # 7% стоп
            }
            # 3x плечо: liq ~ 67.0 (33% от 100), раньше 5 * 7 = 35% давало ложный EXCHANGE_LIQUIDATION_DISTANCE_LOW
            spm.paged_get = MagicMock(side_effect=lambda ep, params=None: (
                [{"symbol": "SOLUSDT", "side": "Buy", "positionIdx": 0, "size": "1", "avgPrice": "100", "markPrice": "101", "stopLoss": "93", "takeProfit": "138.5", "liqPrice": "67"}]
                if "position/list" in ep else []
            ))
            actions = spm.audit_and_protect_open_positions()
            self.assertFalse(any(a.get("action") == "EXCHANGE_LIQUIDATION_DISTANCE_LOW" for a in actions))
            # Истинно опасная ликвидация (liq=91 при sl=93, запас всего 2% < 3%) по-прежнему ловится
            spm.paged_get = MagicMock(side_effect=lambda ep, params=None: (
                [{"symbol": "SOLUSDT", "side": "Buy", "positionIdx": 0, "size": "1", "avgPrice": "100", "markPrice": "101", "stopLoss": "93", "takeProfit": "138.5", "liqPrice": "91"}]
                if "position/list" in ep else []
            ))
            actions_bad = spm.audit_and_protect_open_positions()
            self.assertTrue(any(a.get("action") == "EXCHANGE_LIQUIDATION_DISTANCE_LOW" for a in actions_bad))

    def test_limit_chaser_links_fallback_order_when_order_link_id_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            spm = MagicMock()
            spm.get_spec.return_value = {"qty_step": 0.01, "min_qty": 0.01, "tick_size": 0.1}
            spm.fmt_step.side_effect = lambda v, step, rd=True: f"{float(v):.2f}"
            spm.ledger = MagicMock()
            spm.signed_post.side_effect = [
                {"retCode": 0, "result": {"orderId": "oid-1"}},
                {"retCode": 0, "result": {"orderId": "oid-2"}},
            ]
            ws = MagicMock()
            ws.get_bbo.return_value = (100.0, 100.1)
            ch = SmartLimitChaser(spm, ws, Path(td))
            ch.max_attempts = 1
            ch.step_wait_sec = 0.0
            ch._status = MagicMock(side_effect=[
                {"orderStatus": "Cancelled", "cumExecQty": "0"},
                {"orderStatus": "Filled", "cumExecQty": "0.10"},
            ])
            with patch("aitrade.execution.limit_chaser.time.sleep"):
                res = ch.execute_chased_entry("BTCUSDT", "Buy", 0.10, 100.0, take_profit=105.0, stop_loss=98.0, order_link_id=None)
            self.assertTrue(res["ok"])
            self.assertEqual(spm.ledger.prepare_order.call_count, 2)
            first_link = spm.ledger.prepare_order.call_args_list[0][0][1]
            second_link = spm.ledger.prepare_order.call_args_list[1][0][1]
            self.assertTrue(first_link)
            self.assertEqual(first_link, second_link)

    def test_btc_regime_guard_uses_ws_fallback_when_rest_down(self) -> None:
        guard = RegimeAndCorrelationGuard()
        guard._last_ok_ts = time.time() - 500.0
        with patch.object(guard, "_update_btc_regime_live", side_effect=RuntimeError("REST timeout")):
            reg = guard.update_btc_regime({"metrics": [{"symbol": "BTCUSDT", "mid_price": 91000.0, "obi_top5_pct": 4.0, "cvd_usd": 10000.0}]})
            self.assertEqual(reg["state"], "NEUTRAL")
            self.assertEqual(reg["btc_price"], 91000.0)
            self.assertTrue(reg["allow_long"])

    def test_x_news_minor_depeg_does_not_trigger_global_panic(self) -> None:
        radar = XNewsSentimentRadar()
        _, _, is_panic_minor = radar._score_text("Small algorithmic token XYZ depegs 3% on thin DEX")
        self.assertFalse(is_panic_minor)
        _, _, is_panic_usdt = radar._score_text("USDT depegs sharply across major exchanges")
        self.assertTrue(is_panic_usdt)

    def test_b2_donchian_requires_fresh_breakout(self) -> None:
        # Строим 40 свечей 1h: плавный рост (EMA20 > EMA50), затем пробой на -2 и повторно выше канала на -1
        bars_4h = [Candle(i * 14400_000, 100 + i * 0.5, 101 + i * 0.5, 99 + i * 0.5, 100.5 + i * 0.5, 100, 10000) for i in range(60)]
        bars_1h = [Candle(i * 3600_000, 100.0, 101.0, 99.0, 100.2, 100, 10000) for i in range(38)]
        # -2 свеча уже пробила 101.0 (close=103.0), -1 свеча продолжает быть выше (close=104.0) -> НЕ свежий пробой
        bars_1h.append(Candle(38 * 3600_000, 100.5, 103.2, 100.0, 103.0, 150, 15000))
        bars_1h.append(Candle(39 * 3600_000, 103.0, 104.5, 102.5, 104.0, 150, 15000))
        sig_stale = generate_donchian_intraday_signal("BTCUSDT", bars_1h, bars_4h=bars_4h)
        self.assertIsNone(sig_stale)


if __name__ == "__main__":
    unittest.main()

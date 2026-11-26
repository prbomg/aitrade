"""Execution uses exchange-confirmed quantities and cancellation, never inferred fills."""
import json, threading, time, uuid
from aitrade.storage import atomic_json
from aitrade.exchange.transport import ExchangeError


class SmartLimitChaser:
    def __init__(self, spm, ws_hub, data_dir, stats_filename: str = "chaser_stats.json"):
        self.spm = spm
        self.ws_hub = ws_hub
        self._lock = threading.RLock()
        self.stats_path = data_dir / stats_filename
        self.enabled = True
        self.strict_post_only = False
        self.max_attempts = 2
        self.step_wait_sec = 1.5
        self.max_chase_drift_pct = 0.60
        default_stats = {
            "total_orders": 0,
            "maker_filled": 0,
            "fallback_taker_filled": 0,
            "fee_saved_usd": 0.0,
            "total_notional_usd": 0.0,
        }
        self.stats = dict(default_stats)
        if self.stats_path.exists():
            try:
                loaded = json.loads(self.stats_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.stats.update(loaded)
            except Exception:
                self.stats = dict(default_stats)

    def _get_live_bbo(self, symbol, fallback_px):
        try:
            return self.ws_hub.get_bbo(symbol, max_age=30)
        except Exception:
            if fallback_px and float(fallback_px) > 0:
                px = float(fallback_px)
                return px * 0.9999, px * 1.0001
            raise

    def _status(self, symbol, order_id):
        for path in ("/v5/order/realtime", "/v5/order/history"):
            rows = self.spm.signed_get(path, {"category": "linear", "symbol": symbol, "orderId": order_id})["result"]["list"]
            if rows:
                return rows[0]
        raise ExchangeError("Статус заявки не подтвержден", uncertain=True)

    def execute_chased_entry(
        self,
        symbol,
        side,
        qty,
        ref_price,
        take_profit=None,
        stop_loss=None,
        leverage=3,
        enable_split_tp=True,
        order_link_id=None,
        position_idx=0,
    ):
        if take_profit is None or stop_loss is None or float(take_profit) <= 0 or float(stop_loss) <= 0:
            raise ValueError("Обязательны положительные take_profit и stop_loss")
        spec = self.spm.get_spec(symbol)
        qs = self.spm.fmt_step(qty, spec["qty_step"], True)
        total = float(qs)
        bid, ask = self._get_live_bbo(symbol, ref_price)
        price = bid if side == "Buy" else ask
        if abs(price / ref_price - 1) * 100 > self.max_chase_drift_pct:
            raise ExchangeError("Цена ушла от сигнала")
        effective_link_id = order_link_id or ("at-" + uuid.uuid4().hex[:30])
        payload = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": qs,
            "price": self.spm.fmt_step(price, spec["tick_size"], side == "Buy"),
            "timeInForce": "PostOnly",
            "positionIdx": position_idx,
            "orderLinkId": effective_link_id,
            "takeProfit": self.spm.fmt_step(take_profit, spec["tick_size"], False),
            "stopLoss": self.spm.fmt_step(stop_loss, spec["tick_size"], False),
            "tpslMode": "Full",
        }
        if getattr(self.spm, "ledger", None):
            self.spm.ledger.prepare_order(payload, effective_link_id)
        r = self.spm.signed_post("/v5/order/create", payload)
        oid = r["result"]["orderId"]
        status = None
        terminal = ("Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated")
        for _ in range(self.max_attempts):
            time.sleep(self.step_wait_sec)
            status = self._status(symbol, oid)
            if status["orderStatus"] in terminal:
                break
        if status["orderStatus"] not in terminal:
            try:
                self.spm.signed_post("/v5/order/cancel", {"category": "linear", "symbol": symbol, "orderId": oid})
            except ExchangeError as e:
                if e.uncertain:
                    raise
            for _ in range(6):
                time.sleep(0.3)
                status = self._status(symbol, oid)
                if status["orderStatus"] in terminal:
                    break
            else:
                raise ExchangeError("Отмена не подтверждена; повторный вход запрещён", uncertain=True)
        filled = float(status.get("cumExecQty") or 0)
        rem = max(0, total - filled)
        used_market = False
        ids = [oid]
        if rem >= spec["min_qty"] and not self.strict_post_only:
            bid, ask = self._get_live_bbo(symbol, ref_price)
            market_px = ask if side == "Buy" else bid
            # Revalidate TP/SL side and drift before fallback; quantity never increases.
            valid = (stop_loss < market_px < take_profit) if side == "Buy" else (take_profit < market_px < stop_loss)
            if valid and abs(market_px / ref_price - 1) * 100 <= self.max_chase_drift_pct:
                fb = dict(
                    payload,
                    orderType="Limit",
                    timeInForce="IOC",
                    qty=self.spm.fmt_step(rem, spec["qty_step"], True),
                    orderLinkId=effective_link_id[:30] + "-m",
                )
                cap = market_px * (1.0015 if side == "Buy" else 0.9985)
                if not (stop_loss < cap < take_profit if side == "Buy" else take_profit < cap < stop_loss):
                    if filled > 0:
                        pass
                    else:
                        raise ExchangeError("IOC price cap outside TP/SL", uncertain=False)
                else:
                    fb["price"] = self.spm.fmt_step(cap, spec["tick_size"], side == "Buy")
                    if getattr(self.spm, "ledger", None):
                        self.spm.ledger.prepare_order(fb, effective_link_id)
                    res = self.spm.signed_post("/v5/order/create", fb)
                    fid = res["result"]["orderId"]
                    ids.append(fid)
                    for _ in range(8):
                        time.sleep(0.3)
                        fs = self._status(symbol, fid)
                        if fs["orderStatus"] in terminal:
                            break
                    else:
                        raise ExchangeError("Рыночное исполнение не подтверждено", uncertain=True)
                    filled += float(fs.get("cumExecQty") or 0)
                    used_market = True
        with self._lock:
            self.stats["total_orders"] = int(self.stats.get("total_orders", 0)) + 1
            stat_key = "fallback_taker_filled" if used_market else "maker_filled"
            self.stats[stat_key] = int(self.stats.get(stat_key, 0)) + (1 if filled > 0 else 0)
            atomic_json(self.stats_path, self.stats)
        return {
            "ok": filled > 0,
            "status": ("FILLED" if filled >= total - 1e-10 else "PARTIAL") if filled > 0 else "REJECTED",
            "qty": str(filled),
            "orderId": ids[-1],
            "orderIds": ids,
            "symbol": symbol,
            "side": side,
            "exec_mode": "LIMIT_WITH_CONFIRMED_FILLS",
            "retMsg": "Подтверждено биржей" if filled > 0 else "Заявка не исполнена",
        }

    def get_summary(self):
        with self._lock:
            snap = dict(self.stats)
        tot = int(snap.get("total_orders", 0))
        mk = int(snap.get("maker_filled", 0))
        return dict(
            snap,
            enabled=self.enabled,
            strict_post_only=self.strict_post_only,
            maker_rate_pct=round(mk / tot * 100, 1) if tot else None,
            statistics_note="Счётчики заявок; точные комиссии и maker/taker — в журнале исполнений",
        )

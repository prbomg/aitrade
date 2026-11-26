# -*- coding: utf-8 -*-
from __future__ import annotations
import hashlib, hmac, json, math, subprocess, time, urllib.parse
from typing import Any, Dict, List
from aitrade.exchange.transport import request, ExchangeError
from aitrade.storage import atomic_json
from aitrade.config import BASE_DIR
import urllib.request, uuid, threading
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

class SmartPositionManager:
    SPEC_HOSTS = ("https://api-demo.bybit.com", "https://api.bybit.com", "https://api.bytick.com")

    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api-demo.bybit.com",
                 breakeven_trigger_pct: float = 0.55, trailing_step_pct: float = 0.40, fee_buffer_pct: float = 0.0012,
                 guard_path: Any = None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.breakeven_trigger_pct = breakeven_trigger_pct
        self.trailing_step_pct = trailing_step_pct
        self.fee_buffer_pct = fee_buffer_pct
        self.guard_lock = threading.RLock()
        self._spec_lock = threading.RLock()
        self.guard_path = guard_path if guard_path is not None else (BASE_DIR / "data" / "position_guard.json")
        self.split_tp_enabled = True
        self.ledger = None
        self._spec_cache: Dict[str, Dict[str, float]] = {}

    def signed_get(self, endpoint, params):
        return request(self.base_url, self.api_key, self.api_secret, "GET", endpoint, params)

    def signed_post(self, endpoint, body):
        body = dict(body)
        if endpoint == "/v5/order/create":
            body.setdefault("orderLinkId", "at-" + uuid.uuid4().hex[:30])
        if endpoint == "/v5/order/create" and self.ledger:
            self.ledger.prepare_order(body)
        try:
            result=request(self.base_url, self.api_key, self.api_secret, "POST", endpoint, body)
            if endpoint == "/v5/order/create" and self.ledger:
                self.ledger.order_result(body['orderLinkId'],'ACK',result.get('result',{}))
            return result
        except ExchangeError as exc:
            if endpoint == '/v5/order/create' and self.ledger:
                self.ledger.order_result(body['orderLinkId'],'UNCERTAIN' if exc.uncertain or exc.code==110072 else 'REJECTED',{'error':str(exc)[:200]})
            if endpoint == "/v5/order/create" and (exc.uncertain or exc.code == 110072):
                for path in ("/v5/order/realtime", "/v5/order/history"):
                    try:
                        rows = self.signed_get(path, {"category":"linear", "symbol":body["symbol"], "orderLinkId":body["orderLinkId"]})["result"]["list"]
                        if rows:
                            if self.ledger:self.ledger.order_result(body['orderLinkId'],rows[0]['orderStatus'],rows[0])
                            return {"retCode":0,"retMsg":"Reconciled", "result":{"orderId":rows[0]["orderId"], "orderLinkId":body["orderLinkId"]}}
                    except ExchangeError: pass
            raise

    def paged_get(self, endpoint, params):
        rows=[];params=dict(params);seen=set()
        for _ in range(200):
            result=self.signed_get(endpoint, params)["result"]
            if not isinstance(result.get("list"),list):raise ExchangeError("Некорректный список Bybit")
            rows.extend(result["list"]);cursor=result.get("nextPageCursor")
            if not cursor:return rows
            if cursor in seen:raise ExchangeError("Повтор cursor Bybit")
            seen.add(cursor);params["cursor"]=cursor
        raise ExchangeError("Превышен предел пагинации")

    def get_spec(self, symbol):
        with self._spec_lock:
            if symbol in self._spec_cache:
                return dict(self._spec_cache[symbol])
        qs = urllib.parse.urlencode({"category": "linear", "symbol": symbol})
        hosts = [self.base_url] + [h for h in self.SPEC_HOSTS if h != self.base_url]
        data = None
        last_exc = None
        for host in hosts:
            url = f"{host}/v5/market/instruments-info?{qs}"
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    cand = json.load(r)
                if isinstance(cand, dict) and cand.get("retCode") == 0 and cand.get("result", {}).get("list"):
                    data = cand
                    break
                last_exc = ExchangeError("Нет спецификации " + symbol)
            except Exception as exc:
                last_exc = exc
        if data is None:
            raise last_exc if isinstance(last_exc, ExchangeError) else ExchangeError(f"Нет спецификации {symbol}: {type(last_exc).__name__ if last_exc else 'error'}")
        info=data["result"]["list"][0];lot=info["lotSizeFilter"];pr=info["priceFilter"]
        if info.get("status")!="Trading":raise ExchangeError("Инструмент недоступен для торговли")
        spec={"qty_step":float(lot["qtyStep"]),"min_qty":float(lot["minOrderQty"]),"max_qty":float(lot.get("maxMktOrderQty") or lot["maxOrderQty"]),"min_notional":float(lot.get("minNotionalValue") or 5),"tick_size":float(pr["tickSize"])}
        with self._spec_lock:
            self._spec_cache[symbol] = spec
        return dict(spec)

    @staticmethod
    def fmt_step(val: float, step: float, round_down: bool = True) -> str:
        if not math.isfinite(val) or not math.isfinite(step) or val < 0 or step <= 0:
            raise ValueError("Некорректный размер/шаг")
        dstep=Decimal(str(step)); units=(Decimal(str(val))/dstep).to_integral_value(rounding=ROUND_DOWN if round_down else ROUND_HALF_UP)
        return format(units*dstep, 'f')

    def place_tp1_half_limit(self, symbol: str, position_side: str, total_size: float, tp1_price: float, position_idx: int = 0, order_link_id: str = "", target_qty=None) -> Dict[str, Any]:
        spec = self.get_spec(symbol)
        half_qty = min(total_size,float(target_qty)) if target_qty is not None else total_size * 0.5
        qty_str = self.fmt_step(half_qty, spec["qty_step"], round_down=True)
        if float(qty_str) < spec["min_qty"] or total_size-float(qty_str)<spec["min_qty"]:
            return {"ok":True,"skipped":True,"orderId":"","retMsg":"Позиция слишком мала для разделения"}
        px_str = self.fmt_step(tp1_price, spec["tick_size"], round_down=False)
        close_side = "Sell" if position_side == "Buy" else "Buy"
        resp = self.signed_post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": close_side, "orderType": "Limit",
            "orderLinkId": order_link_id or ("tp-"+uuid.uuid4().hex[:30]),
            "qty": qty_str, "price": px_str, "reduceOnly": True, "timeInForce": "GTC", "positionIdx": int(position_idx)
        })
        return {"ok": resp.get("retCode") == 0, "symbol": symbol, "tp1_qty": qty_str, "tp1_price": px_str,
                "orderId": resp.get("result", {}).get("orderId", ""), "retMsg": resp.get("retMsg", "")}

    def move_sl_to_breakeven(self, symbol: str, position_side: str, avg_entry_price: float, position_idx: int = 0) -> Dict[str, Any]:
        spec = self.get_spec(symbol)
        be_px = avg_entry_price * (1.0 + self.fee_buffer_pct) if position_side == "Buy" else avg_entry_price * (1.0 - self.fee_buffer_pct)
        be_str = self.fmt_step(be_px, spec["tick_size"], round_down=False)
        resp = self.signed_post("/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol, "stopLoss": be_str, "tpslMode": "Full", "positionIdx": int(position_idx)
        })
        return {"ok": resp.get("retCode") == 0, "symbol": symbol, "be_price": be_str, "retMsg": resp.get("retMsg", "")}

    def activate_trailing_stop(self, symbol: str, mark_price: float, trail_pct: float = 0.40, position_idx: int = 0) -> Dict[str, Any]:
        spec = self.get_spec(symbol)
        trail_dist = max(spec["tick_size"] * 2.0, mark_price * (trail_pct / 100.0))
        trail_str = self.fmt_step(trail_dist, spec["tick_size"], round_down=False)
        resp = self.signed_post("/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol, "trailingStop": trail_str, "tpslMode": "Full", "positionIdx": int(position_idx)
        })
        return {"ok": resp.get("retCode") == 0, "symbol": symbol, "trailing_dist": trail_str, "retMsg": resp.get("retMsg", "")}

    def ensure_isolated_margin(self, symbol: str, leverage: int = 3) -> Dict[str, Any]:
        """Подтверждает режим Isolated Margin (поддерживает и Classic/UTA1.0 switch-isolated, и UTA2.0 set-margin-mode)."""
        lev_str = str(int(max(1, min(leverage, 5))))
        try:
            resp = self.signed_post("/v5/position/switch-isolated", {
                "category": "linear",
                "symbol": symbol,
                "tradeMode": 1,
                "buyLeverage": lev_str,
                "sellLeverage": lev_str,
            })
            if resp.get("retCode") == 0:
                return {"ok": True, "symbol": symbol, "mode": "ISOLATED", "retMsg": resp.get("retMsg", "")}
            return {"ok": False, "symbol": symbol, "mode": "UNKNOWN", "retMsg": str(resp.get("retMsg", "margin switch failed"))}
        except ExchangeError as exc:
            # 110026: уже Isolated
            if exc.code == 110026 or "110026" in str(exc):
                return {"ok": True, "symbol": symbol, "mode": "ISOLATED", "retMsg": "Already isolated"}
            # На Bybit Demo (api-demo.bybit.com) код 10032 ("Demo trading is not supported") — ограничение эндпоинта на Demo
            if "api-demo.bybit.com" in self.base_url and (exc.code == 10032 or "10032" in str(exc) or "demo trading" in str(exc).lower()):
                return {"ok": True, "symbol": symbol, "mode": "DEMO_DEFAULT", "retMsg": "Bybit Demo 10032: margin-mode endpoint unsupported on Demo"}
            # Только явная ошибка UTA 2.0 (100028); 110044 (нехватка маржи) не должна менять режим всего аккаунта
            if exc.code == 100028 or "uta" in str(exc).lower() or "unified" in str(exc).lower():
                try:
                    u_resp = self.signed_post("/v5/account/set-margin-mode", {"setMarginMode": "ISOLATED_MARGIN"})
                    if u_resp.get("retCode") == 0:
                        return {"ok": True, "symbol": symbol, "mode": "ISOLATED_MARGIN", "retMsg": u_resp.get("retMsg", "UTA isolated")}
                except ExchangeError as u_exc:
                    if "api-demo.bybit.com" in self.base_url and (u_exc.code == 10032 or "10032" in str(u_exc)):
                        return {"ok": True, "symbol": symbol, "mode": "DEMO_DEFAULT", "retMsg": "Bybit Demo 10032: UTA margin-mode unsupported on Demo"}
                    return {"ok": False, "symbol": symbol, "mode": "UNKNOWN", "retMsg": str(u_exc)[:150]}
            return {"ok": False, "symbol": symbol, "mode": "UNKNOWN", "retMsg": str(exc)[:150]}

    def load_guard(self) -> Dict[str, Any]:
        with self.guard_lock:
            if not self.guard_path.exists():
                return {}
            try:
                data = json.loads(self.guard_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

    def audit_and_protect_open_positions(self):
        with self.guard_lock:
            return self._audit_positions()

    def _audit_positions(self):
        positions=[p for p in self.paged_get("/v5/position/list",{"category":"linear","settleCoin":"USDT","limit":200}) if float(p.get("size") or 0)>0]
        orders=self.paged_get("/v5/order/realtime",{"category":"linear","settleCoin":"USDT","limit":50})
        guard=self.load_guard()
        actions=[];active=set()
        for p in positions:
            sym=p["symbol"];side=p["side"];idx=int(p.get("positionIdx") or 0);size=float(p["size"]);avg=float(p["avgPrice"]);mark=float(p["markPrice"])
            intent=self.ledger.active_intent(sym,side,size) if self.ledger else None
            if not intent:
                if float(p.get('stopLoss') or 0)<=0:actions.append({'ok':False,'symbol':sym,'action':'UNOWNED_MISSING_STOP_LOSS'})
                continue
            key=self.base_url+":"+sym+":"+str(idx)+":"+intent['intent_id'];active.add(key)
            record=guard.setdefault(key,{"tp1_link":"tp-"+uuid.uuid4().hex[:30],"tp1_order":"","tp1_done":False})
            record.setdefault("tp1_target_qty",size*.3)
            record.setdefault("tp1_filled_qty",0.)
            try:
                if float(p.get("stopLoss") or 0)<=0:
                    actions.append({"ok":False,"symbol":sym,"action":"MISSING_STOP_LOSS"})
                    continue
                liq=float(p.get('liqPrice') or 0)
                sl_ref=float(intent.get('sl') or p['stopLoss'])
                sl_dist=abs(avg-sl_ref)
                min_liq_dist=max(2.2*sl_dist, sl_dist + avg*0.03)
                if liq>0 and abs(avg-liq)<min_liq_dist:
                    actions.append({'ok':False,'symbol':sym,'action':'EXCHANGE_LIQUIDATION_DISTANCE_LOW'})
                own=[o for o in orders if int(o.get("positionIdx") or 0)==idx and o.get("symbol")==sym and o.get("orderLinkId")==record["tp1_link"]]
                if record.get("tp1_order") and not own and not record["tp1_done"]:
                    hist=self.signed_get("/v5/order/history",{"category":"linear","symbol":sym,"orderId":record["tp1_order"]})["result"]["list"]
                    if hist:
                        st = hist[0].get("orderStatus")
                        cum_q = float(hist[0].get("cumExecQty") or 0)
                        if st in ('Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated'):
                            record['tp1_filled_qty']+=cum_q
                            record['tp1_done']=record['tp1_filled_qty']>=record['tp1_target_qty']-1e-10
                            if not record['tp1_done']:
                                record['tp1_order']=''
                                record['tp1_link']='tp-'+uuid.uuid4().hex[:30]
                strat = intent["strategy_id"]
                pure_sltp_ids = (
                    "S1_TREND_RETEST", "B1_SQUEEZE_BREAKOUT", "B2_DONCHIAN_INTRADAY",
                    "B3_VWAP_MOMENTUM_IMPULSE", "B4_ORB_SESSION_BREAKOUT", "B5_OI_TREND_EXPANSION",
                    "B6_EMA_PULLBACK_TREND", "B7_FUNDING_SQUEEZE_MOMENTUM",
                    "T1_DONCHIAN_TSMOM", "T2_RS_BTC_BREAKOUT", "T3_KELTNER_TREND_4H",
                    "C2_MTF_COMBO_BREAKOUT", "C3_ENSEMBLE_VOTE_2OF3",
                )
                is_s1 = any(x in strat for x in pure_sltp_ids)
                is_s3 = "S3_VWAP_RANGE" in strat
                is_booster = strat.startswith("BOOSTER:") or any(x in strat for x in pure_sltp_ids)
                if self.split_tp_enabled and not (is_s1 or is_s3) and not record["tp1_order"] and not record["tp1_done"]:
                    tp=float(p.get("takeProfit") or 0)
                    if tp<=0:raise ExchangeError("Нет подтверждённого TP для TP1")
                    atomic_json(self.guard_path,guard)
                    r=self.place_tp1_half_limit(sym,side,size,(avg + max(abs(avg - float(p.get('stopLoss') or 0)), 0.5 * abs(tp - avg)) if side == 'Buy' else avg - max(abs(avg - float(p.get('stopLoss') or 0)), 0.5 * abs(tp - avg))),idx,record["tp1_link"],target_qty=max(0.,record["tp1_target_qty"]-record["tp1_filled_qty"]))
                    record["tp1_order"]=r["orderId"]
                    if r.get("skipped"):record["tp1_done"]=True
                    actions.append({"ok":True,"symbol":sym,"action":"TP1_30%"})
                move=((mark-avg)/avg if side=="Buy" else (avg-mark)/avg)*100
                sl=float(p.get("stopLoss") or 0);be=(sl>=avg if side=="Buy" else 0<sl<=avg)
                if sl>0 and not be and "initial_sl" not in record:
                    record["initial_sl"]=float(intent.get("sl") or sl)
                init_sl=float(record.get("initial_sl") or sl)
                risk_pct=(abs(avg-init_sl)/avg)*100.0 if (avg>0 and init_sl>0) else self.breakeven_trigger_pct
                be_trigger=max(self.breakeven_trigger_pct,risk_pct*1.0)
                # S1/B1 и S3 работают по чистому SL/TP без раннего БУ/трейлинга (1:1 с WFA MFE/MAE)
                if not (is_s3 or is_s1) and move>=be_trigger:
                    if not be:
                        self.move_sl_to_breakeven(sym,side,avg,idx);actions.append({"ok":True,"symbol":sym,"action":"BREAKEVEN"})
                    atr15_raw=float(((intent.get("signal_context") or {}).get("features") or {}).get("atr15") or 0.0)
                    atr15_pct=(atr15_raw/avg*100.0) if (atr15_raw>0 and avg>0) else 0.0
                    trail_ready = (move >= risk_pct * (1.8 if is_booster else 1.3))
                    trail_step = max(self.trailing_step_pct, 1.5, 1.8 * atr15_pct) if is_booster else max(self.trailing_step_pct, 0.85)
                    if trail_ready and float(p.get("trailingStop") or 0)<=0:
                        self.activate_trailing_stop(sym,mark,trail_step,idx);actions.append({"ok":True,"symbol":sym,"action":"TRAILING"})
            except Exception as exc:
                actions.append({"ok":False,"symbol":sym,"action":"PROTECTION_ERROR","error":str(exc)[:200]})
        guard={k:v for k,v in guard.items() if k in active or not k.startswith(self.base_url+":")}
        atomic_json(self.guard_path,guard)
        return actions

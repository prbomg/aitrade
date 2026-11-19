# -*- coding: utf-8 -*-
from __future__ import annotations
from aitrade.storage import atomic_json
import json, subprocess, threading, time, math, uuid, copy, traceback
from aitrade.storage import Ledger
from aitrade.exchange.transport import ExchangeError
from concurrent.futures import ThreadPoolExecutor
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from wsgiref.simple_server import make_server, WSGIRequestHandler

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.booster.oi_squeeze_scanner import OISqueezeScanner
from aitrade.booster.regime_correlation_guard import RegimeAndCorrelationGuard
from aitrade.booster.ml_calibrator import AdaptiveMLCalibrator
from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.booster.env_manager import BybitEnvManager
from aitrade.config import BASE_DIR, load_settings
from aitrade.engine import AITradeCombineEngine
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.execution.limit_chaser import SmartLimitChaser


STATE_FILE = BASE_DIR / "data" / "ui_state.json"


class QuietWSGIHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class BybitV5DirectController:
    def __init__(self, engine: AITradeCombineEngine) -> None:
        self.engine = engine
        self.base_url = "https://api-demo.bybit.com"
        self.spm = SmartPositionManager(engine.client.api_key, engine.client.api_secret, self.base_url)
        self.ledger = Ledger(BASE_DIR / 'data' / 'execution_ledger.sqlite3')
        self.spm.ledger = self.ledger
        self._entry_lock = threading.RLock()
        self._history_lock = threading.RLock()
        self._history_ts = 0.0

    def attach_chaser(self, chaser: SmartLimitChaser) -> None:
        self.chaser = chaser

    def apply_credentials(self, api_key: str, api_secret: str, base_url: str) -> None:
        self.base_url = base_url
        self.engine.client.api_key = api_key
        self.engine.client.api_secret = api_secret
        self.engine.client.trade_base_url = base_url
        self.engine.client.mode = "demo" if "api-demo" in base_url else "live"
        self.ledger.environment = "demo" if "api-demo" in base_url else "mainnet"
        self._history_ts = 0.0
        self.spm.api_key = api_key
        self.spm.api_secret = api_secret
        self.spm.base_url = base_url

    def get_account_summary(self) -> Dict[str, Any]:
        res = self.spm.signed_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        lst = res.get("result", {}).get("list", [])
        if not lst:
            raise ExchangeError("Пустой ответ баланса")
        acct = lst[0]
        total_eq = float(acct.get("totalEquity", 0) or 0)
        total_upnl = float(acct.get("totalPerpUPL", 0) or 0)
        usdt_bal = total_eq
        for c in acct.get("coin", []):
            if c.get("coin") == "USDT":
                usdt_bal = float(c.get("walletBalance", 0) or c.get("equity", 0) or total_eq)
                break
        return {"total_equity": round(total_eq, 2), "usdt_balance": round(usdt_bal, 2), "unrealised_pnl": round(total_upnl, 2)}

    def get_positions(self):
        rows=self.spm.paged_get('/v5/position/list',{'category':'linear','settleCoin':'USDT','limit':200})
        positions=[r for r in rows if float(r.get('size') or 0)>0]
        for r in positions:
            intent=self.ledger.active_intent(r['symbol'],r.get('side'),r.get('size'))
            strategy=intent['strategy_id'] if intent else 'UNATTRIBUTED'
            r['entry_intent_id']=intent['intent_id'] if intent else ''
            r['opened_ms']=intent['opened_ms'] if intent else 0
            r['entry_created_ms']=int(intent['created']*1000) if intent else 0
            r['campaign_id']=intent.get('campaign_id') if intent else None
            r['strategy_id']=strategy
            r['source_tag']='🚀 Booster A+' if strategy.startswith('BOOSTER:') else ('🎯 ML Core' if strategy.startswith('CORE:') else 'Внешняя / ручная')
        return positions

    def get_open_orders(self):
        return self.spm.paged_get('/v5/order/realtime',{'category':'linear','settleCoin':'USDT','limit':50})

    def _reconcile_intent(self, intent_id, symbol, stored):
        children=self.ledger.orders_for(intent_id)
        if not children:
            # Pre-journal ambiguous chaser errors cannot be cleared by querying only a parent.
            if stored.get('error') and not stored.get('orderIds') and not stored.get('orderId'):return
            children=[{'orderLinkId':intent_id,'orderId':oid} for oid in (stored.get('orderIds') or [stored.get('orderId','')])]
        terminal={'Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated'}
        total=0.;all_terminal=True;missing=False;ids=[]
        for child in children:
            if child.get('journal_status')=='REJECTED':continue
            q={'category':'linear','symbol':symbol}
            if child.get('orderId'):q['orderId']=child['orderId']
            else:q['orderLinkId']=child['orderLinkId']
            found=None
            for endpoint in ('/v5/order/realtime','/v5/order/history'):
                rows=self.spm.signed_get(endpoint,q)['result']['list']
                if rows:found=rows[0];break
            if not found:missing=True;continue
            ids.append(found['orderId']);total+=float(found.get('cumExecQty') or 0)
            all_terminal=all_terminal and found.get('orderStatus') in terminal
            self.ledger.order_result(child['orderLinkId'],found.get('orderStatus','UNKNOWN'),found)
        status='UNCERTAIN' if missing else ('PENDING' if not all_terminal else ('FILLED' if total>=float(stored.get('requestedQty',stored.get('qty',0)) or 0)-1e-10 and total>0 else ('PARTIAL' if total>0 else 'REJECTED')))
        self.ledger.finish(intent_id,status,{'confirmedQty':total,'orderIds':ids})

    def reconcile_pending(self):
        if not self._entry_lock.acquire(blocking=False):return
        try:
            with self.ledger.connect() as c:
                rows=c.execute("SELECT id,symbol,payload FROM intents WHERE environment=? AND status IN ('SUBMITTING','UNCERTAIN','PENDING')",(self.ledger.environment,)).fetchall()
            for iid,symbol,raw in rows:self._reconcile_intent(iid,symbol,json.loads(raw))
            for order in self.ledger.pending_exit_orders():
                q={'category':'linear','symbol':order['symbol']}
                q.update({'orderId':order['orderId']} if order.get('orderId') else {'orderLinkId':order['orderLinkId']})
                for endpoint in ('/v5/order/realtime','/v5/order/history'):
                    found=self.spm.signed_get(endpoint,q)['result']['list']
                    if found:
                        self.ledger.order_result(order['orderLinkId'],found[0].get('orderStatus','UNKNOWN'),found[0]);break
        finally:self._entry_lock.release()

    def reconcile_and_clear_unresolved(self):
        with self._entry_lock:self.reconcile_pending()

    def get_closed_pnl(self):
        self.reconcile_pending()
        with self._history_lock:
            if time.time()-self._history_ts>10:
                now=int(time.time()*1000)
                rows=self.spm.paged_get('/v5/position/closed-pnl',{'category':'linear','limit':100,'startTime':now-7*86400000+1,'endTime':now})
                self.ledger.put_records('closed',rows,'orderId')
                for kind,path,key,limit in [('executions','/v5/execution/list','execId',100),('transactions','/v5/account/transaction-log','id',50)]:
                    params={'category':'linear','limit':limit,'startTime':now-7*86400000+1 if self._history_ts==0 else now-3600000,'endTime':now}
                    if kind=='transactions':params.update(accountType='UNIFIED',currency='USDT')
                    self.ledger.put_records(kind,self.spm.paged_get(path,params),key)
                self._history_ts=time.time()
            return sorted(self.ledger.attribute(self.ledger.records('closed')),key=lambda r:int(r.get('updatedTime') or 0),reverse=True)

    def place_order(self,symbol,side,order_type,qty,price=None,take_profit=None,stop_loss=None,leverage=3,enable_split_tp=True,strategy_id='MANUAL',signal_context=None):
        with self._entry_lock:
            return self._place_checked(symbol,side,order_type,qty,price,take_profit,stop_loss,leverage,enable_split_tp,strategy_id,signal_context)

    def pyramid_position(self,*args,**kwargs):
        return {'ok':False,'retMsg':'Доливка отключена: сначала требуется отдельный расчёт совокупного риска'}

    def close_position(self, symbol: str, side: str, size: str, position_idx: int = 0) -> Dict[str, Any]:
        with self._entry_lock:
            return self._close_position_locked(symbol,side,size,position_idx)

    def _close_position_locked(self, symbol, side, size, position_idx):
        close_side = "Sell" if side == "Buy" else "Buy"
        # Unknown market-close submission survives restart; do not submit another close blindly.
        for pending in self.ledger.pending_exit_orders():
            if pending['symbol']==symbol and int(pending.get('positionIdx') or 0)==int(position_idx) and pending.get('orderType')=='Market':
                return {'ok':True,'status':'PENDING','orderId':pending.get('orderId'),'retMsg':'Предыдущая заявка закрытия ожидает подтверждения; повтор не отправлен'}
        resp = self.spm.signed_post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": close_side, "orderType": "Market",
            "qty": str(size), "reduceOnly": True, "timeInForce": "IOC", "positionIdx": int(position_idx)
        })
        return {"ok": resp.get("retCode") == 0, "status":"PENDING", "orderId":resp.get("result",{}).get("orderId"), "retMsg":"Заявка закрытия принята; исполнение ещё не подтверждено"}

    def close_half_position(self, symbol: str, side: str, size: str, position_idx: int = 0) -> Dict[str, Any]:
        spec = self.spm.get_spec(symbol)
        half_sz = min(float(size or 0), max(spec["min_qty"], float(size or 0) * 0.5))
        qty_str = self.spm.fmt_step(half_sz, spec["qty_step"], True)
        return self.close_position(symbol, side, qty_str, position_idx)

    def close_all_positions(self) -> Dict[str, Any]:
        positions = self.get_positions()
        results=[]
        for p in positions:
            results.append(self.close_position(p.get("symbol", ""), p.get("side", "Buy"), str(p.get("size", "0")), int(p.get("positionIdx", 0) or 0)))
        return {"ok": all(r.get("ok") for r in results), "submitted_count": len(results)}

    def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        resp = self.spm.signed_post("/v5/order/cancel", {"category": "linear", "symbol": symbol, "orderId": order_id})
        return {"ok": resp.get("retCode") == 0, "retMsg": resp.get("retMsg")}

    def cancel_all_orders(self) -> Dict[str, Any]:
        resp = self.spm.signed_post("/v5/order/cancel-all", {"category": "linear", "settleCoin": "USDT"})
        return {"ok": resp.get("retCode") == 0, "retMsg": resp.get("retMsg")}

    def _place_checked(self,symbol,side,order_type,qty,price,tp,sl,leverage,split_tp,strategy_id,signal_context=None):
        intent_id=None
        submitted=False
        try:
            if self.base_url!='https://api-demo.bybit.com':raise ValueError('Mainnet заблокирован до отдельной проверки')
            owner=getattr(self,'owner',None)
            if owner and (strategy_id.startswith('BOOSTER') or strategy_id.startswith('CORE')):
                if owner.entry_paused.is_set():raise ValueError('Новые входы на паузе')
            if symbol not in self.engine.settings['universe']['core_pairs'] or side not in ('Buy','Sell') or order_type not in ('Market','Limit'):raise ValueError('Некорректные параметры заявки')
            qty,tp,sl=float(qty),float(tp),float(sl)
            if price is not None and (not math.isfinite(float(price)) or float(price)<=0):raise ValueError('Некорректная цена')
            vals=[qty,tp,sl]
            if any(v is None or not math.isfinite(float(v)) or float(v)<=0 for v in vals):raise ValueError('Обязательны положительные qty, TP и SL')
            if not 1<=int(leverage)<=3:raise ValueError('Плечо ограничено 3x')
            self.reconcile_pending()
            if self.ledger.unresolved(symbol):raise ValueError('Есть неподтверждённая заявка; требуется сверка')
            positions=self.get_positions();orders=[o for o in self.get_open_orders() if not o.get('reduceOnly')]
            busy={p['symbol'] for p in positions}|{o['symbol'] for o in orders}
            if symbol in busy or len(busy)>=3:raise ValueError('Лимит позиций/заявок или повторный символ')
            midnight=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000
            booster_obj=getattr(owner,'booster',None) if owner else None
            b_state=getattr(booster_obj,'state',None)
            if isinstance(b_state,dict):
                reset_ms=max(int(b_state.get('limits_reset_at_ms',0) or 0),int(b_state.get('started_at_ms',0) or 0))
                cutoff_ms=max(midnight,float(reset_ms))
                training_mode=bool(b_state.get('training_unlimited_mode',False))
            else:
                cutoff_ms=midnight
                training_mode=False
            if not training_mode and self.ledger.today_count(since_ts=cutoff_ms/1000.0)>=10:raise ValueError('Лимит 10 входов в день')
            capital=min(1000.,self.get_account_summary()['usdt_balance'])
            if strategy_id.startswith('BOOSTER') and booster_obj:
                metrics=booster_obj.compute_metrics(self.get_closed_pnl(),positions)
                if metrics.get('goal_reached'):raise ValueError('Цель кампании достигнута; новые входы остановлены')
                capital=min(capital,metrics['risk_capital_usd'])
            if capital<=0:raise ValueError('Нет подтверждённого капитала')
            daily=sum(float(c['closedPnl']) for c in self.get_closed_pnl() if int(c['updatedTime'])>=cutoff_ms)
            upnl=sum(float(p.get('unrealisedPnl') or 0) for p in positions)
            if not training_mode and upnl<=-capital*.01:raise ValueError('Плавающий убыток > 1%: новые входы заблокированы')
            if not training_mode and daily+min(upnl,0)<=-capital*.03:raise ValueError('Дневной лимит -3%: новые входы заблокированы')
            if owner:
                bid,ask=owner.ws_hub.get_bbo(symbol,5);ref=ask if side=='Buy' else bid
                spread_bps=((ask-bid)/((ask+bid)*0.5))*10000.0 if (bid>0 and ask>0) else 0.0
                max_spread=8.0 if symbol in ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT') else 20.0
                if spread_bps>max_spread:raise ValueError(f'Спред {spread_bps:.1f} bps > {max_spread:.0f} bps')
                fr_map=getattr(owner,'funding_rates',None)
                if not isinstance(fr_map,dict) or symbol not in fr_map or time.time()-getattr(owner,'funding_updated_at',0)>600:raise ValueError('Нет свежего фандинга: обновите скан')
                fr_val=float(fr_map[symbol])
                if (side=='Buy' and fr_val>0.0005) or (side=='Sell' and fr_val<-0.0005):
                    raise ValueError('Экстремальный фандинг против стороны входа > 0.05%')
                ws_info=owner.ws_hub.get_summary()
                ws_warm=bool(ws_info.get('flow_window_complete',False)) or training_mode or (time.time()-getattr(owner,'started_at',0)>=120 and int(ws_info.get('symbols_live',0) or 0)>0)
                if strategy_id.startswith('BOOSTER') and not ws_warm:raise ValueError('Поток CVD/ликвидаций прогревается после подключения (2 мин)')
                permitted,reason=owner.regime_guard.can_open_position(symbol,side,positions,owner.ws_hub.get_summary())
                if not permitted:raise ValueError(reason)
            else:raise ValueError('Нет подтвержденного потока рынка')
            max_dev=0.05 if order_type=='Limit' else 0.003
            if price and abs(float(price)/ref-1)>max_dev:raise ValueError('Сигнал устарел: цена изменилась')
            ref=float(price) if order_type=='Limit' and price else ref
            if not (sl<ref<tp if side=='Buy' else tp<ref<sl):raise ValueError('Неверное расположение SL/TP')
            if abs(ref-sl)/ref<0.0055:raise ValueError('Стоп-лосс уже 0.55%: издержки съедают R:R')
            min_rr_req=0.50 if 'S3_VWAP_RANGE' in str(strategy_id) else 1.5
            if abs(tp-ref)/abs(ref-sl)<min_rr_req:raise ValueError(f'R:R ниже {min_rr_req}')
            spec=self.spm.get_spec(symbol)
            sl=float(self.spm.fmt_step(sl,spec['tick_size'],False));tp=float(self.spm.fmt_step(tp,spec['tick_size'],False))
            if not (sl<ref<tp if side=='Buy' else tp<ref<sl):raise ValueError('SL/TP после округления невалидны')
            risk_per_coin=abs(ref-sl)+ref*(.0011+.0015+.0028)
            c_w=float((signal_context or {}).get('cal_weight',1.0) or 1.0)
            risk_frac=max(0.0035,min(0.0070,0.005*c_w)) if (strategy_id.startswith('BOOSTER') or strategy_id.startswith('CORE')) else 0.005
            maxqty=capital*risk_frac/risk_per_coin
            used_notional=sum(float(p['size'])*float(p.get('markPrice') or p['avgPrice']) for p in positions)+sum(float(o.get('leavesQty') or o['qty'])*float(o.get('price') or 0) for o in orders)
            available_notional=max(0.,capital*3-used_notional)
            qty=float(self.spm.fmt_step(min(float(qty),maxqty,available_notional/ref,spec['max_qty']),spec['qty_step'],True))
            if qty<spec['min_qty'] or qty*ref<max(spec['min_notional'],24):raise ValueError('Размер после ограничения риска ниже минимума')
            totalrisk=qty*risk_per_coin
            for p in positions:
                ep=float(p['avgPrice']);psl=float(p.get('stopLoss') or 0)
                if psl<=0:raise ValueError('Открытая позиция без SL')
                totalrisk+=float(p['size'])*(abs(ep-psl)+ep*.0054)
            for o in orders:
                ep=float(o.get('price') or 0);osl=float(o.get('stopLoss') or 0)
                if not ep or not osl:raise ValueError('Непроверенный риск активной заявки')
                totalrisk+=float(o.get('leavesQty') or o['qty'])*(abs(ep-osl)+ep*.0054)
            if totalrisk>capital*.02:raise ValueError('Совокупный риск выше 2%')
            if owner and (strategy_id.startswith('BOOSTER') or strategy_id.startswith('CORE')) and owner.entry_paused.is_set():raise ValueError('Вход остановлен пользователем')
            intent_id='at-'+uuid.uuid4().hex[:30]
            self.ledger.intent(intent_id,symbol,strategy_id,qty*risk_per_coin,{'side':side,'qty':qty,'requestedQty':qty,'campaign_id':str(b_state.get('started_at_ms',0)) if isinstance(b_state,dict) and strategy_id.startswith('BOOSTER') else None,'strategy_version':'intraday.2','signal_context':signal_context or {},'entry':ref,'sl':sl,'tp':tp,'exit_plan':{'version':1,'max_hold_minutes':360 if 'S1_TREND_RETEST' in strategy_id else (240 if 'S3_VWAP_RANGE' in strategy_id else (180 if strategy_id.startswith('BOOSTER') else 0))}})
            self.spm.signed_post('/v5/position/set-leverage',{'category':'linear','symbol':symbol,'buyLeverage':str(leverage),'sellLeverage':str(leverage)})
            # Read position mode explicitly, never resubmit the same entry to another positionIdx.
            modes=self.spm.paged_get('/v5/position/list',{'category':'linear','symbol':symbol})
            hedge=any(int(p.get('positionIdx') or 0)>0 for p in modes)
            idx=(1 if side=='Buy' else 2) if hedge else 0
            chaser=getattr(self,'chaser',None)
            if chaser and chaser.enabled and order_type=='Market':
                submitted=True
                result=chaser.execute_chased_entry(symbol,side,qty,ref,tp,sl,leverage,split_tp,intent_id,idx)
            else:
                payload={'category':'linear','symbol':symbol,'side':side,'qty':str(qty),'orderType':order_type,'positionIdx':idx,'orderLinkId':intent_id,'timeInForce':'GTC' if order_type=='Limit' else 'IOC','stopLoss':self.spm.fmt_step(sl,spec['tick_size'],False),'takeProfit':self.spm.fmt_step(tp,spec['tick_size'],False),'tpslMode':'Full'}
                if order_type=='Limit':payload['price']=self.spm.fmt_step(ref,spec['tick_size'],side=='Buy')
                else:
                    cap=ref*(1.0015 if side=='Buy' else .9985)
                    if not (sl<cap<tp if side=='Buy' else tp<cap<sl):raise ValueError('Лимит исполнения выходит за SL/TP')
                    payload.update(orderType='Limit',timeInForce='IOC',price=self.spm.fmt_step(cap,spec['tick_size'],side=='Buy'))
                submitted=True
                response=self.spm.signed_post('/v5/order/create',payload)
                result={'ok':True,'status':'PENDING','orderId':response['result']['orderId'],'qty':str(qty),'symbol':symbol,'side':side,'retMsg':'Заявка принята; исполнение ожидается'}
            self.ledger.finish(intent_id,result.get('status','PENDING'),result)
            return result
        except Exception as exc:
            uncertain=submitted or (isinstance(exc,ExchangeError) and exc.uncertain)
            if intent_id:self.ledger.finish(intent_id,'UNCERTAIN' if uncertain else 'REJECTED',{'error':str(exc)[:200]})
            self.ledger.event('ENTRY_BLOCKED',{'symbol':symbol,'strategy':strategy_id,'reason':str(exc)[:200]})
            return {'ok':False,'symbol':symbol,'retMsg':str(exc)[:200]}


class ControlCenterState:
    def __init__(self) -> None:
        self.started_at=time.time()
        manifest_path=BASE_DIR/"release-manifest.json"
        self.release_manifest=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        self.settings = load_settings()
        self.engine = AITradeCombineEngine(self.settings)
        self.bybit = BybitV5DirectController(self.engine)
        self.bybit.owner = self
        self.ws_hub = BybitV5WebSocketHub(self.settings.get("universe", {}).get("core_pairs", []))
        self.ws_hub.start()
        self.limit_chaser = SmartLimitChaser(self.bybit.spm, self.ws_hub, BASE_DIR / "data")
        self.bybit.attach_chaser(self.limit_chaser)
        self.booster = DepositBoosterManager(BASE_DIR / "data")
        self.oi_scanner = OISqueezeScanner()
        self.regime_guard = RegimeAndCorrelationGuard()
        self.ml_calibrator = AdaptiveMLCalibrator(BASE_DIR / "data")
        self.x_radar = XNewsSentimentRadar()
        self.env_manager = BybitEnvManager(BASE_DIR / "data", self.engine.client.api_key, self.engine.client.api_secret)
        active_creds = self.env_manager.get_active_credentials()
        if active_creds.get("api_key"):
            self.bybit.apply_credentials(active_creds["api_key"], active_creds["api_secret"], active_creds["base_url"])
        self.backtest_cache: Dict[str, Any] = {}
        self.last_scan: Dict[str, Any] = {}

        saved = self._load_persisted_state()
        self.autopilot_booster_enabled = bool(saved.get("autopilot_booster_enabled", False))
        self.autopilot_core_enabled = bool(saved.get("autopilot_core_enabled", False))
        self.autopilot_interval_sec = int(saved.get("autopilot_interval_sec", 300) or 300)
        self.booster_cycles_done = int(saved.get("booster_cycles_done", 0) or 0)
        self.core_cycles_done = int(saved.get("core_cycles_done", 0) or 0)
        self._last_auto_ts = 0.0

        self.smart_guard_enabled = bool(saved.get("smart_guard_enabled", True))
        self.split_tp_enabled = bool(saved.get("split_tp_enabled", True))
        self.limit_chaser.enabled = bool(saved.get("limit_chaser_enabled", True))
        self.limit_chaser.strict_post_only = bool(saved.get("limit_chaser_strict", False))
        self.bybit.spm.breakeven_trigger_pct = float(saved.get("breakeven_trigger_pct", 0.55) or 0.55)
        self.bybit.spm.trailing_step_pct = float(saved.get("trailing_step_pct", 0.40) or 0.40)
        self.max_leverage = int(saved.get("max_leverage", self.settings.get("risk", {}).get("max_leverage", 3)) or 3)
        self.core_max_positions = int(saved.get("core_max_positions", 3) or 3)
        self.core_min_prob_pct = float(saved.get("core_min_prob_pct", 58.0) or 58.0)
        self.sound_alerts_enabled = bool(saved.get("sound_alerts_enabled", True))

        self.regime_guard.enabled = bool(saved.get("btc_guard_enabled", True))
        self.regime_guard.max_same_sector = int(saved.get("max_same_sector", 1) or 1)
        self.regime_guard.max_same_direction_total = int(saved.get("max_same_direction_total", 2) or 2)
        self.regime_guard.time_stop_enabled = bool(saved.get("time_stop_enabled", True))
        self.regime_guard.time_stop_minutes = int(saved.get("time_stop_minutes", 45) or 45)
        self.regime_guard.time_stop_band_pct = float(saved.get("time_stop_band_pct", 0.25) or 0.25)

        self.position_sources: Dict[str, str] = saved.get("position_sources", {})
        self.position_opened_ts: Dict[str, float] = saved.get("position_opened_ts", {})

        self.manual_prefs = saved.get("manual_prefs", {
            "symbol": "BTCUSDT", "order_type": "Market", "qty": 0.02, "tp_pct": 1.6, "sl_pct": 0.8,
        })

        self.last_scan_ts = "—"
        self.logs: List[str] = []
        self._lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._snapshot = {}
        self._snapshot_ts = 0.0
        self._snapshot_error = "Первый снимок загружается"
        self._snapshot_lock = threading.Lock()
        self.entry_paused = threading.Event()
        if not (self.autopilot_core_enabled or self.autopilot_booster_enabled): self.entry_paused.set()
        self.scan_status = {"running":False,"last_error":"","last_success":0.,"last_attempt":0.,"heartbeat":time.time(),"failed_cycles":0}
        self._scan_executor = ThreadPoolExecutor(max_workers=1)
        self._manual_future = None
        self.manual_task={"id":"","status":"idle","error":""}
        self.guard_status={"heartbeat":time.time(),"last_success":0.,"last_error":""}
        self.funding_rates={}
        self.funding_updated_at=0.
        self.add_log(
            "🛡 Модуль BTC Regime & Correlation Guard + Time-Stop ("
            + str(self.regime_guard.time_stop_minutes) + "м) активирован!"
        )
        threading.Thread(target=self._init_background, daemon=True).start()
        self._autopilot_thread = threading.Thread(target=self._autopilot_loop, name="autopilot", daemon=True)
        self._autopilot_thread.start()
        threading.Thread(target=self._snapshot_loop, name="state-cache", daemon=True).start()
        threading.Thread(target=self._smart_guard_loop, daemon=True).start()
        threading.Thread(target=self._news_loop, daemon=True).start()

    def _load_persisted_state(self) -> Dict[str, Any]:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save_persisted_state(self) -> None:
        payload = {
            "autopilot_booster_enabled": self.autopilot_booster_enabled,
            "autopilot_core_enabled": self.autopilot_core_enabled,
            "autopilot_interval_sec": self.autopilot_interval_sec,
            "booster_cycles_done": self.booster_cycles_done,
            "core_cycles_done": self.core_cycles_done,
            "smart_guard_enabled": self.smart_guard_enabled,
            "split_tp_enabled": self.split_tp_enabled,
            "limit_chaser_enabled": self.limit_chaser.enabled,
            "limit_chaser_strict": self.limit_chaser.strict_post_only,
            "breakeven_trigger_pct": self.bybit.spm.breakeven_trigger_pct,
            "trailing_step_pct": self.bybit.spm.trailing_step_pct,
            "max_leverage": self.max_leverage,
            "core_max_positions": self.core_max_positions,
            "core_min_prob_pct": self.core_min_prob_pct,
            "sound_alerts_enabled": self.sound_alerts_enabled,
            "btc_guard_enabled": self.regime_guard.enabled,
            "max_same_sector": self.regime_guard.max_same_sector,
            "max_same_direction_total": self.regime_guard.max_same_direction_total,
            "time_stop_enabled": self.regime_guard.time_stop_enabled,
            "time_stop_minutes": self.regime_guard.time_stop_minutes,
            "time_stop_band_pct": self.regime_guard.time_stop_band_pct,
            "position_sources": self.position_sources,
            "position_opened_ts": self.position_opened_ts,
            "manual_prefs": self.manual_prefs,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        atomic_json(STATE_FILE, payload)

    def mark_position_source(self, symbol: str, source_tag: str) -> None:
        self.position_sources[symbol] = source_tag
        self.position_opened_ts[symbol] = time.time()
        self.save_persisted_state()

    def get_position_age_minutes(self, pos: Dict[str, Any]) -> float:
        sym = str(pos.get("symbol", ""))
        now = time.time()
        if pos.get("opened_ms"):return round(max(0.,(now*1000-pos["opened_ms"])/60000),1)
        if sym in self.position_opened_ts and self.position_opened_ts[sym] > 0:
            return round(max(0.0, (now - float(self.position_opened_ts[sym])) / 60.0), 1)
        c_ms = int(pos.get("createdTime", 0) or pos.get("updatedTime", 0) or 0)
        if c_ms > 0:
            open_ts = c_ms / 1000.0
            age_m = (now - open_ts) / 60.0
            if 0 <= age_m <= 1440:
                self.position_opened_ts[sym] = open_ts
                return round(age_m, 1)
        self.position_opened_ts[sym] = now
        return 0.1

    def _init_background(self) -> None:
        last_backup_day = ""
        while True:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != last_backup_day:
                try:
                    bdir = BASE_DIR / "data" / "backups"
                    self.bybit.ledger.backup_to(bdir, keep=7)
                    if STATE_FILE.exists():
                        atomic_json(bdir / f"ui_state-{today}.json", json.loads(STATE_FILE.read_text(encoding="utf-8")))
                    last_backup_day = today
                except Exception:
                    pass
            try:
                bt = WalkForwardBacktester(self.settings).run_comparison_backtest()
                bt["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.backtest_cache = bt
            except Exception as exc:
                self.backtest_cache = {"status": "error", "note": str(exc)[:200]}
            time.sleep(3600)

    def add_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.insert(0, "[" + ts + "] " + str(msg))
        self.logs = self.logs[:120]
        print("AItrade " + str(msg), flush=True)
        self.bybit.ledger.event("LOG", {"message": str(msg)[:1000]})

    def _news_loop(self):
        while True:
            try:self.x_radar.refresh_feed()
            except Exception as exc:self.add_log('Ошибка обновления новостей: '+str(exc)[:150])
            time.sleep(60)

    def _smart_guard_loop(self) -> None:
        while True:
            time.sleep(5)
            self.guard_status["heartbeat"]=time.time()
            if not self.smart_guard_enabled:
                continue
            try:
                self.bybit.get_closed_pnl()
                self.bybit.spm.split_tp_enabled = self.split_tp_enabled
                actions = self.bybit.spm.audit_and_protect_open_positions()
                for act in actions:
                    if not act.get("ok"):
                        self.add_log("ОШИБКА ЗАЩИТЫ: " + str(act))
                        self.entry_paused.set()
                    if act.get("ok"):
                        self.add_log("🛡 Smart Guard (фон): " + str(act.get("action")) + " для " + str(act.get("symbol")))

                now_ms = int(time.time() * 1000)
                for o in self.bybit.get_open_orders():
                    if bool(o.get("reduceOnly")) or o.get("orderType") != "Limit":
                        continue
                    o_sym = str(o.get("symbol", ""))
                    o_side = str(o.get("side", ""))
                    o_strat = self.bybit.ledger.pending_strategy(o_sym, o_side)
                    c_ms = int(o.get("createdTime", 0) or 0)
                    ttl_min = 45 if "S1_TREND_RETEST" in o_strat else (20 if o_strat.startswith("BOOSTER:") else 0)
                    if ttl_min > 0 and c_ms > 0 and (now_ms - c_ms) >= ttl_min * 60 * 1000:
                        c_ord = self.bybit.cancel_order(o_sym, str(o.get("orderId", "")))
                        if c_ord.get("ok"):
                            self.add_log(f"⏱ [Limit TTL] Отменён неисполненный лимит {o_sym} (>{ttl_min} мин)")

                positions = self.bybit.get_positions()
                active_syms = {str(p.get("symbol", "")) for p in positions}
                for old_s in list(self.position_opened_ts.keys()):
                    if old_s not in active_syms:
                        del self.position_opened_ts[old_s]

                # News network refresh runs independently of position protection.

                for p in positions:
                    sym = str(p.get("symbol", ""))
                    side = str(p.get("side", "Buy"))
                    size_str = str(p.get("size", "0"))
                    avg_px = float(p.get("avgPrice", 0) or 0)
                    mark_px = float(p.get("markPrice", 0) or avg_px)
                    cur_sl = float(p.get("stopLoss", 0) or 0)
                    p_idx = int(p.get("positionIdx", 0) or 0)
                    if avg_px <= 0 or not p.get('entry_intent_id'):
                        continue
                    intent=self.bybit.ledger.active_intent(sym,side,size_str)
                    plan=(intent or {}).get('exit_plan',{})
                    hold=plan.get('max_hold_minutes',360 if 'S1_TREND_RETEST' in str(p.get('strategy_id')) else (240 if 'S3_VWAP_RANGE' in str(p.get('strategy_id')) else (180 if str(p.get('strategy_id','')).startswith('BOOSTER') else 0)))
                    if hold and p.get('opened_ms') and (time.time()*1000-p['opened_ms'])>=hold*60000:
                        pending=[o for o in self.bybit.get_open_orders() if o.get('symbol')==sym and o.get('reduceOnly') and o.get('orderType')=='Market']
                        if not pending:
                            self.bybit.close_position(sym,side,size_str,p_idx)
                            self.add_log('⏱ Лимит времени стратегии: отправлено закрытие '+sym)
                        continue
                    move_pct = ((mark_px - avg_px) / avg_px) * 100.0 if side == "Buy" else ((avg_px - mark_px) / avg_px) * 100.0
                    is_be = (side == "Buy" and cur_sl >= avg_px) or (side == "Sell" and 0 < cur_sl <= avg_px)

                    # 1. Защита и закрытие открытых позиций по радару новостей X/News
                    if hasattr(self, "x_radar") and self.x_radar.enabled and hasattr(self.x_radar, "evaluate_open_position"):
                        n_eval = self.x_radar.evaluate_open_position(sym, side)
                        n_act = n_eval.get("action", "HOLD")
                        if n_act == "CLOSE_FULL":
                            c_res = self.bybit.close_position(sym, side, size_str, p_idx)
                            if c_res.get("ok"):
                                self.add_log("🚨 [News Guard] Экстренное закрытие " + sym + " (" + side + "): " + str(n_eval.get("reason")))
                            continue
                        elif n_act == "PROTECT" and not is_be and move_pct >= 0.18:
                            p_strat = str(p.get("strategy_id") or self.bybit.ledger.latest_strategy(sym, side))
                            if not any(x in p_strat for x in ("S1_TREND_RETEST","S3_VWAP_RANGE")):
                                be_res = self.bybit.spm.move_sl_to_breakeven(sym, side, avg_px, p_idx)
                                self.bybit.spm.activate_trailing_stop(sym, mark_px, self.bybit.spm.trailing_step_pct, p_idx)
                                if be_res.get("ok"):
                                    self.add_log("🛡 [News Guard] Досрочный БУ + Трейлинг для " + sym + ": " + str(n_eval.get("reason")))
                                    is_be = True

                    # 2. Time-Stop для сделок без импульса
                    if self.regime_guard.time_stop_enabled and not is_be and not any(x in str(p.get("strategy_id")) for x in ("S1_TREND_RETEST","S3_VWAP_RANGE","BOOSTER:")):
                        age_min = self.get_position_age_minutes(p)
                        if age_min >= self.regime_guard.time_stop_minutes and abs(move_pct) <= self.regime_guard.time_stop_band_pct:
                            c_res = self.bybit.close_position(sym, side, size_str, p_idx)
                            if c_res.get("ok"):
                                self.add_log(
                                    "⏱ Time-Stop сработал для " + sym + ": сделка висела " + str(age_min)
                                    + " мин без импульса (" + "{:+.2f}%".format(move_pct) + ") — отправлена заявка на закрытие."
                                )
                self.guard_status.update(last_success=time.time(),last_error='')
            except Exception as exc:
                self.guard_status['last_error']=str(exc)[:200]
                self.entry_paused.set()
                self.add_log("Ошибка Smart Guard: " + str(exc)[:200])
            finally:self.guard_status['heartbeat']=time.time()

    def _autopilot_loop(self):
        while True:
            self.scan_status["heartbeat"] = time.time()
            time.sleep(1)
            if not (self.autopilot_booster_enabled or self.autopilot_core_enabled): continue
            if self.entry_paused.is_set(): continue
            if time.time()-self._last_auto_ts < self.autopilot_interval_sec: continue
            self._last_auto_ts = time.time()
            b,c=self.autopilot_booster_enabled,self.autopilot_core_enabled
            try:
                self.run_scan(trade_booster=b,trade_core=c)
                if b:self.booster_cycles_done+=1
                if c:self.core_cycles_done+=1
                self.save_persisted_state()
            except Exception as exc:
                self.add_log("Ошибка цикла; следующий цикл сохранён: " + str(exc)[:250])
            finally:
                self.scan_status["heartbeat"]=time.time()

    def _snapshot_loop(self):
        while True:
            try:
                value=self.get_full_state()
                with self._snapshot_lock:
                    self._snapshot=value;self._snapshot_ts=time.time();self._snapshot_error=""
            except Exception as exc:
                self._snapshot_error=str(exc)[:200]
                self.add_log("Ошибка обновления данных: " + self._snapshot_error)
            time.sleep(5)

    def refresh_booster_snapshot(self):
        with self._snapshot_lock:
            closed_list = self._snapshot.get("closed_pnl") or []
            pos_list = self._snapshot.get("positions") or []
            if hasattr(self, "booster"):
                b_metrics = self.booster.compute_metrics(closed_list, pos_list)
                start_eq = float(b_metrics.get("start_equity", 1000.0) or 1000.0)
                # Equity curve is rebuilt from the same confirmed campaign ledger.
                self._snapshot["booster"] = b_metrics

    def health(self):
        now=time.time();scan=dict(self.scan_status)
        alive=self._autopilot_thread.is_alive()
        guard=getattr(self,'guard_status',{})
        age=now-self._snapshot_ts if self._snapshot_ts else None
        liveness=alive and (not scan.get('running') or now-scan.get('last_attempt',0)<240) and now-guard.get('heartbeat',0)<120
        reasons=[]
        if age is None or age>90 or self._snapshot_error:reasons.append('Снимок биржи устарел или недоступен')
        if guard.get('last_error'):reasons.append('Ошибка сопровождения позиций')
        if scan.get('failed_cycles',0)>=3 and scan.get('last_error'):reasons.append('Повторные ошибки сканирования')
        requested=getattr(self,'autopilot_core_enabled',False) or getattr(self,'autopilot_booster_enabled',False)
        if requested and not self.entry_paused.is_set() and now-(scan.get('last_success') or getattr(self,'started_at',now))>max(240,2*getattr(self,'autopilot_interval_sec',300)+60):reasons.append('Нет успешных циклов в допустимый срок')
        if not liveness:reasons.append('Остановлен или завис рабочий поток')
        return {"ok":liveness and not reasons,"liveness_ok":liveness,"version":"2026.11.17-intraday.2",
                "build_sha256":getattr(self,"release_manifest",{}).get("build_sha256"),
                "autopilot_alive":alive,"entry_paused":self.entry_paused.is_set(),"scan":scan,
                "guard":guard,"snapshot_age_sec":round(age,1) if age is not None else None,
                "snapshot_error":self._snapshot_error,"readiness_errors":reasons}

    def cached_state(self):
        with self._snapshot_lock:result=copy.deepcopy(self._snapshot)
        result['health']=self.health()
        result['manual_task']=dict(self.manual_task)
        ap=result.setdefault('autopilot',{})
        ap.update(booster_enabled=self.autopilot_booster_enabled,core_enabled=self.autopilot_core_enabled,any_enabled=self.autopilot_booster_enabled or self.autopilot_core_enabled,booster_cycles_done=self.booster_cycles_done,core_cycles_done=self.core_cycles_done,next_in_sec=max(0,int(self.autopilot_interval_sec-(time.time()-self._last_auto_ts))))
        if hasattr(self, 'booster') and isinstance(result.get('booster'), dict):
            try:
                b_metrics = self.booster.compute_metrics(result.get('closed_pnl') or [], result.get('positions') or [])
                # Keep curve and HWM from one calculation.
                result['booster'] = b_metrics
            except Exception:
                pass
        result['logs']=list(self.logs)
        return result

    def run_scan(self, trade_booster=False, trade_core=False):
        with self._lock:
            self.scan_status.update(running=True,last_attempt=time.time(),last_error="")
            try:
                r=self._run_scan(trade_booster,trade_core)
                self.scan_status['last_success']=time.time()
                self.scan_status['failed_cycles']=0
                return r
            except Exception as exc:
                self.scan_status['failed_cycles']+=1
                self.scan_status['last_error']=str(exc)[:250]
                raise
            finally:self.scan_status['running']=False

    def _run_scan(self, trade_booster: bool = False, trade_core: bool = False) -> Dict[str, Any]:
        with self._lock:
            try:
                self.funding_rates = {
                    str(t["symbol"]): float(t.get("fundingRate", 0) or 0)
                    for t in (self.engine.client.get_tickers() or [])
                    if t.get("symbol") and t.get("fundingRate") not in (None, "")
                }
                self.funding_updated_at=time.time()
            except Exception:
                pass
            core_pairs = self.settings.get("universe", {}).get("core_pairs", [])
            ws_sum = self.ws_hub.get_summary()
            btc_reg = self.regime_guard.update_btc_regime(ws_sum)
            news_sum = self.x_radar.refresh_feed()
            squeeze_list = self.oi_scanner.scan_squeeze_setups(core_pairs, ws_sum)
            for s in squeeze_list:
                s["sector"] = self.regime_guard.get_sector(s.get("symbol", ""))

            scan_res = self.engine.scan_market(auto_execute_demo=False)
            scan_res["oi_squeeze_signals"] = squeeze_list
            scan_res["btc_regime"] = btc_reg

            positions = self.bybit.get_positions()
            closed_pnl = self.bybit.get_closed_pnl()
            cal_summary = self.ml_calibrator.calibrate_from_closed_pnl(closed_pnl)
            if self.ml_calibrator.state.get("enabled", True):
                self.core_min_prob_pct = float(cal_summary.get("recommended_ml_threshold_pct", self.core_min_prob_pct))
            b_metrics = self.booster.compute_metrics(closed_pnl, positions)

            # Обогащаем сигналы OI Squeeze и ИИ-ансамбля весами из AdaptiveMLCalibrator
            for s in squeeze_list:
                mod = self.ml_calibrator.get_strategy_modifier(s.get("setup_type", ""))
                n_mod = self.x_radar.get_coin_modifier(s.get("symbol", ""), s.get("direction", "LONG"))
                s["cal_weight"] = mod.get("weight", 1.0)
                s["cal_status"] = mod.get("status", "ACTIVE")
                s["cal_badge"] = mod.get("badge", "")
                s["news_tag"] = n_mod["tag"]
                news_penalty = min(0.0, float(n_mod.get("score_bonus", 0) or 0))
                w_factor = 1.0 if float(s["cal_weight"]) >= 1.0 else (0.85 + 0.15 * float(s["cal_weight"]))
                base_sc = float(s.get("score", 50.0)) * w_factor + news_penalty
                s["score"] = round(max(10.0, min(99.0, base_sc)), 1)
                if n_mod["tag"]:
                    s["rationale"] = str(s.get("rationale", "")) + " | " + n_mod["tag"]
                if s["score"] >= 74.0 and s["cal_status"] != "QUARANTINE" and n_mod["allowed"]:
                    s["grade"] = "A+"
                elif not n_mod["allowed"] or s["cal_status"] == "QUARANTINE":
                    s["grade"] = "B"

            for lst_key in ("approved_signals", "rejected_signals"):
                for sig in (scan_res.get(lst_key) or []):
                    mod = self.ml_calibrator.get_strategy_modifier(sig.get("strategy_id") or sig.get("strategy_name", ""))
                    sig["direction"] = "LONG" if sig.get("side") == "Buy" else "SHORT"
                    n_mod = self.x_radar.get_coin_modifier(sig.get("symbol", ""), sig["direction"])
                    raw_p = float(sig.get("ml_win_probability", 0.5) or 0.5)
                    news_prob_delta = -0.08 if float(n_mod.get("score_bonus", 0) or 0) < 0 else 0.0
                    adj_p = max(0.05, min(0.99, raw_p + news_prob_delta))
                    sig["ml_win_probability"] = round(adj_p, 4)
                    sig["news_tag"] = n_mod.get("tag", "")
                    sig["cal_weight"] = mod.get("weight", 1.0)
                    sig["cal_status"] = mod.get("status", "ACTIVE")
                    sig["cal_badge"] = mod.get("badge", "")
                    base_coins = float(sig.get("position_qty", 0) or 0)
                    sig["position_size_coins"] = round(base_coins * min(1.0, mod["weight"]), 8)
                    sig["take_profit"] = sig.get("take_profit_2") or sig.get("take_profit_1")
            order_type = self.settings.get("execution", {}).get("order_type", "Market")

            if trade_booster and self.booster.state.get("enabled", True) and not self.entry_paused.is_set():
                if b_metrics["circuit_breaker_tripped"]:
                    self.add_log("🛑 [Разгон $1k→$10k] Сработал дневной стоп-кран (-3%). Входы разгона приостановлены.")
                else:
                    booster_slots = max(0, int(b_metrics["max_positions"]) - len(positions))
                    a_plus_list = [s for s in squeeze_list if s.get("grade") == "A+" and s.get("cal_status") != "QUARANTINE"]
                    for top_sq in a_plus_list:
                        if booster_slots <= 0:
                            break
                        side = "Buy" if top_sq["direction"] == "LONG" else "Sell"
                        on_cd, cd_left = self.booster.is_symbol_on_cooldown(top_sq["symbol"])
                        if on_cd:
                            self.add_log("⏳ [Кулдаун после стопа] Пропуск " + top_sq["symbol"] + ": карантин еще " + str(cd_left) + " мин")
                            continue
                        allowed, guard_msg = self.regime_guard.can_open_position(top_sq["symbol"], side, positions, ws_sum)
                        n_mod = self.x_radar.get_coin_modifier(top_sq["symbol"], top_sq["direction"])
                        if not allowed or not n_mod["allowed"]:
                            why = guard_msg if not allowed else n_mod["reason"]
                            self.add_log("🛡 [Разгон Фильтр] Пропуск " + top_sq["symbol"] + " (" + side + "): " + why)
                            continue
                        sizing = self.booster.calculate_order_qty(top_sq["entry_price"], top_sq["stop_loss"], b_metrics)
                        w_mult = float(top_sq.get("cal_weight", 1.0) or 1.0)
                        adj_qty = round(sizing["qty_coins"] * w_mult, 6)
                        sq_otype = top_sq.get("order_type", order_type)
                        r = self.bybit.place_order(
                            top_sq["symbol"], side, sq_otype, adj_qty,
                            top_sq["entry_price"], top_sq["take_profit"], top_sq["stop_loss"],
                            sizing["leverage"], self.split_tp_enabled, strategy_id="BOOSTER:" + top_sq.get("setup_type", "UNKNOWN"),
                            signal_context={"features": {"vwap_zscore": 0.0, "rsi_14": 50.0, "rvol": 1.5, "clv_proxy": float(top_sq.get("obi_top5_pct", 0.0)) / 100.0}, "score": top_sq.get("score"), "cal_weight": w_mult}
                        )
                        if not r.get("ok"): self.add_log("Вход Booster отклонён: " + str(r.get("retMsg")))
                        if r.get("ok"):
                            booster_slots -= 1
                            positions = self.bybit.get_positions()
                            self.mark_position_source(top_sq["symbol"], "🚀 Booster A+")
                            self.booster.register_booster_symbol(top_sq["symbol"])
                            self.ml_calibrator.register_entry(top_sq["symbol"], top_sq.get("setup_type", "SHORT SQUEEZE BREAKOUT"))
                            self.add_log(
                                "🚀 [Автопилот Разгона] Открыт " + top_sq["symbol"] + " " + side
                                + " [" + guard_msg + "] qty=" + str(r.get("qty")) + ", риск $" + str(sizing["risk_usd"])
                            )
                    if booster_slots > 0 and not trade_core:
                        s1_cands = [s for s in (scan_res.get("approved_signals") or []) if s.get("strategy_id") == "S1_TREND_RETEST" and s.get("passed_ml_filter") and s.get("debate_verdict") == "APPROVED"]
                        for s1 in s1_cands:
                            if booster_slots <= 0:
                                break
                            side = s1["side"]
                            allowed, guard_msg = self.regime_guard.can_open_position(s1["symbol"], side, positions, ws_sum)
                            n_mod = self.x_radar.get_coin_modifier(s1["symbol"], "LONG" if side == "Buy" else "SHORT")
                            if not allowed or not n_mod["allowed"]:
                                continue
                            sizing = self.booster.calculate_order_qty(float(s1["entry_price"]), float(s1["stop_loss"]), b_metrics)
                            w_mult = float(s1.get("cal_weight", 1.0) or 1.0)
                            adj_qty = round(sizing["qty_coins"] * w_mult, 6)
                            r = self.bybit.place_order(
                                s1["symbol"], side, "Limit", adj_qty,
                                float(s1["entry_price"]), float(s1.get("take_profit") or s1["take_profit_2"]), float(s1["stop_loss"]),
                                sizing["leverage"], False, strategy_id="BOOSTER:S1_TREND_RETEST",
                                signal_context={"features": s1.get("features", {}), "ml_score": s1.get("ml_win_probability"), "cal_weight": w_mult, "version": "intraday.2"}
                            )
                            if r.get("ok"):
                                booster_slots -= 1
                                positions = self.bybit.get_positions()
                                self.mark_position_source(s1["symbol"], "🚀 Booster A+")
                                self.booster.register_booster_symbol(s1["symbol"])
                                self.ml_calibrator.register_entry(s1["symbol"], "S1_TREND_RETEST")
                                self.add_log("🚀 [Разгон S1 Retest] Выставлен лимит " + s1["symbol"] + " " + side + " qty=" + str(r.get("qty")))

            if trade_core and not self.entry_paused.is_set():
                positions = self.bybit.get_positions()
                core_slots = max(0, self.core_max_positions - len(positions))
                min_prob = self.core_min_prob_pct / 100.0
                all_candidates = (scan_res.get("approved_signals", []) or [])
                valid_core = [s for s in all_candidates if s.get("passed_ml_filter") and s.get("debate_verdict") == "APPROVED" and s.get("cal_status") != "QUARANTINE" and float(s.get("ml_win_probability", 0) or 0) >= (0.52 if s.get("strategy_id") == "S1_TREND_RETEST" else (0.56 if s.get("strategy_id") == "S3_VWAP_RANGE" else min_prob))]
                valid_core.sort(key=lambda x: float(x.get("ml_win_probability", 0) or 0), reverse=True)
                for sig in valid_core:
                    if core_slots <= 0:
                        break
                    side = sig["side"]
                    if side not in ("Buy", "Sell"): raise ValueError("Invalid signal side")
                    allowed, guard_msg = self.regime_guard.can_open_position(sig["symbol"], side, positions, ws_sum)
                    n_mod = self.x_radar.get_coin_modifier(sig["symbol"], sig.get("direction", sig.get("side", "LONG")))
                    if not allowed or not n_mod["allowed"]:
                        why = guard_msg if not allowed else n_mod["reason"]
                        self.add_log("🛡 [Основная ИИ Фильтр] Пропуск " + str(sig["symbol"]) + " (" + side + "): " + why)
                        continue
                    sig_otype = sig.get("order_type") if sig.get("order_type") in ("Market", "Limit") else order_type
                    sig_split_tp = False if sig.get("strategy_id") in ("S1_TREND_RETEST", "S3_VWAP_RANGE") else self.split_tp_enabled
                    r = self.bybit.place_order(
                        sig["symbol"], side, sig_otype, float(sig.get("position_size_coins", 0) or 0),
                        float(sig.get("entry_price", 0) or 0), float(sig.get("take_profit", 0) or 0),
                        float(sig.get("stop_loss", 0) or 0), self.max_leverage, sig_split_tp, strategy_id="CORE:" + str(sig.get("strategy_id", "UNKNOWN")),signal_context={"features":sig.get("features",{}),"ml_score":sig.get("ml_win_probability"),"cal_weight":sig.get("cal_weight",1.0),"version":"intraday.2"}
                    )
                    if not r.get("ok"): self.add_log("Вход Core отклонён: " + str(r.get("retMsg")))
                    if r.get("ok"):
                        core_slots -= 1
                        positions = self.bybit.get_positions()
                        self.mark_position_source(sig["symbol"], "🎯 ML Core")
                        self.ml_calibrator.register_entry(sig["symbol"], str(sig.get("strategy_id") or sig.get("strategy_name", "S1_TREND_RETEST")))
                        self.add_log(
                            "🎯 [Автопилот Основной ИИ] Открыт " + str(sig["symbol"]) + " " + side
                            + " [" + guard_msg + ", ML " + str(round(float(sig.get("ml_win_probability", 0)) * 100, 1)) + "%]"
                        )

            self.last_scan = scan_res
            self.last_scan_ts = datetime.now().strftime("%H:%M:%S")
            return scan_res

    def get_full_state(self) -> Dict[str, Any]:
        acct = self.bybit.get_account_summary()
        closed_pnl = self.bybit.get_closed_pnl()
        positions = self.bybit.get_positions()
        open_orders = self.bybit.get_open_orders()
        ws_sum = self.ws_hub.get_summary()
        btc_reg = self.regime_guard.update_btc_regime(ws_sum)

        for p in positions:
            sym = p.get("symbol", "")
            side = p.get("side", "Buy")
            avg_px = float(p.get("avgPrice", 0) or 0)
            cur_sl = float(p.get("stopLoss", 0) or 0)
            cur_trail = float(p.get("trailingStop", 0) or 0)
            tp1_ord = next((o for o in open_orders if o.get("symbol") == sym and bool(o.get("reduceOnly"))), None)
            p["tp1_order_price"] = tp1_ord.get("price", "") if tp1_ord else ""
            p["tp1_order_qty"] = tp1_ord.get("qty", "") if tp1_ord else ""
            p["is_breakeven"] = (side == "Buy" and cur_sl >= avg_px) or (side == "Sell" and 0 < cur_sl <= avg_px)
            p["has_trailing"] = cur_trail > 0
            p["source_tag"] = p.get("source_tag", "Внешняя / ручная")
            p["sector"] = self.regime_guard.get_sector(sym)
            p["age_min"] = self.get_position_age_minutes(p)

        realized_sum = sum(float(c.get("closedPnl", 0) or 0) for c in closed_pnl)
        wins = sum(1 for c in closed_pnl if float(c.get("closedPnl", 0) or 0) > 0)
        win_rate = round((wins / len(closed_pnl)) * 100.0, 1) if closed_pnl else 0.0

        any_active = self.autopilot_booster_enabled or self.autopilot_core_enabled
        next_in_sec = 0
        if any_active and self._last_auto_ts > 0:
            next_in_sec = max(0, int(self.autopilot_interval_sec - (time.time() - self._last_auto_ts)))

        booster_metrics = self.booster.compute_metrics(closed_pnl, positions)


        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_scan_ts": self.last_scan_ts,
            "account": acct,
            "env_status": self.env_manager.get_public_status(),
            "realized_pnl_recent": round(realized_sum, 2),
            "closed_trades_count": len(closed_pnl),
            "live_win_rate_pct": win_rate,
            "positions": positions,
            "open_orders": open_orders,
            "closed_pnl": closed_pnl[:100],
            "ws": ws_sum,
            "btc_regime": btc_reg,
            "x_news": self.x_radar.refresh_feed(),
            "ml_calibration": self.ml_calibrator.calibrate_from_closed_pnl(closed_pnl),
            "regime_guard_config": {
                "enabled": self.regime_guard.enabled,
                "max_same_sector": self.regime_guard.max_same_sector,
                "max_same_direction_total": self.regime_guard.max_same_direction_total,
                "time_stop_enabled": self.regime_guard.time_stop_enabled,
                "time_stop_minutes": self.regime_guard.time_stop_minutes,
                "time_stop_band_pct": self.regime_guard.time_stop_band_pct,
            },
            "booster": booster_metrics,
            "oi_squeeze": self.oi_scanner.cached_signals,
            "scan": self.last_scan,
            "backtest": self.backtest_cache,
            "settings": {
                "order_type": self.settings.get("execution", {}).get("order_type", "Market"),
                "max_leverage": self.max_leverage,
                "core_max_positions": self.core_max_positions,
                "core_min_prob_pct": self.core_min_prob_pct,
                "sound_alerts_enabled": self.sound_alerts_enabled,
                "core_pairs": self.settings.get("universe", {}).get("core_pairs", []),
            },
            "manual_prefs": self.manual_prefs,
            "limit_chaser": self.limit_chaser.get_summary(),
            "smart_guard": {
                "enabled": self.smart_guard_enabled,
                "split_tp_enabled": self.split_tp_enabled,
                "breakeven_trigger_pct": self.bybit.spm.breakeven_trigger_pct,
                "trailing_step_pct": self.bybit.spm.trailing_step_pct,
            },
            "autopilot": {
                "booster_enabled": self.autopilot_booster_enabled,
                "core_enabled": self.autopilot_core_enabled,
                "any_enabled": any_active,
                "interval_sec": self.autopilot_interval_sec,
                "booster_cycles_done": self.booster_cycles_done,
                "core_cycles_done": self.core_cycles_done,
                "next_in_sec": next_in_sec,
            },
            "logs": self.logs,
        }


def create_wsgi_app(state: ControlCenterState):
    html_file = BASE_DIR / "dashboard_report.html"

    def app(environ: Dict[str, Any], start_response: Any):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        headers = [
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Cache-Control", "no-store"),
        ]
        if method == "GET" and path == "/healthz":
            health=state.health();body=json.dumps(health).encode()
            start_response("200 OK" if health["ok"] else "503 Service Unavailable", [("Content-Type","application/json")])
            return [body]
        if method == "OPTIONS":
            start_response("403 Forbidden", headers + [("Content-Type", "text/plain")])
            return [b"Cross-origin requests disabled"]
        if method == "GET" and path in ("/", "/index.html"):
            body = html_file.read_bytes() if html_file.exists() else b"<h1>Loading...</h1>"
            start_response("200 OK", headers + [("Content-Type", "text/html; charset=utf-8")])
            return [body]
        if method == "GET" and path == "/api/state":
            payload = json.dumps(state.cached_state(), ensure_ascii=False).encode("utf-8")
            start_response("200 OK", headers + [("Content-Type", "application/json; charset=utf-8")])
            return [payload]
        if method == "POST":
            if environ.get("HTTP_X_REQUESTED_WITH") != "AItrade": raise ValueError("Требуется заголовок клиента")
            origin=environ.get("HTTP_ORIGIN")
            if origin and urlsplit(origin).netloc != environ.get("HTTP_HOST"):raise ValueError("Недопустимый Origin")
            if environ.get("CONTENT_TYPE", "").split(";",1)[0] != "application/json":raise ValueError("Требуется application/json")
            length = int(environ.get("CONTENT_LENGTH", 0) or 0)
            if length<0 or length>65536:raise ValueError("Недопустимый размер запроса")
            raw = environ["wsgi.input"].read(length).decode("utf-8") if length > 0 else "{}"
            data = json.loads(raw or "{}")
            if not isinstance(data,dict):raise ValueError("Ожидается объект JSON")
            resp: Dict[str, Any] = {"ok": True}

            if path == "/api/env_control":
                act = data.get("action", "")
                if act == "save_keys":
                    t_mode = data.get("target_mode", "mainnet")
                    k_val = str(data.get("api_key", "") or "").strip()
                    s_val = str(data.get("api_secret", "") or "").strip()
                    resp = state.env_manager.save_keys(t_mode, k_val, s_val)
                    # Если обновили ключи текущего активного режима — сразу применяем
                    creds = state.env_manager.get_active_credentials()
                    if creds["mode"] == t_mode and creds["api_key"]:
                        state.bybit.apply_credentials(creds["api_key"], creds["api_secret"], creds["base_url"])
                    state.add_log("🔐 Ключи для контура [" + t_mode.upper() + "] сохранены в защищенное хранилище (" + str(resp.get("masked_key")) + ").")
                elif act == "run_checklist":
                    t_mode = data.get("target_mode", "mainnet")
                    chk = state.env_manager.run_preflight_checklist(t_mode)
                    state.add_log("📋 Предторговый чек-лист [" + t_mode.upper() + "]: " + str(chk.get("summary")))
                    resp = {"ok": chk.get("passed", False), "checklist": chk, "retMsg": chk.get("summary")}
                elif act == "switch_mode":
                    t_mode = data.get("target_mode", "demo")
                    if t_mode != "demo":raise ValueError("Mainnet отключён до отдельной валидации")
                    state.entry_paused.set()
                    sw = state.env_manager.switch_environment(t_mode)
                    if sw.get("ok"):
                        # При смене контура ставим автопилоты на паузу для безопасности
                        state.autopilot_booster_enabled = False
                        state.autopilot_core_enabled = False
                        creds = state.env_manager.get_active_credentials()
                        state.bybit.apply_credentials(creds["api_key"], creds["api_secret"], creds["base_url"])
                        state.save_persisted_state()
                        state.add_log("🔄 ПЕРЕКЛЮЧЕН КОНТУР БИРЖИ: " + str(sw.get("retMsg")) + " (Автопилоты переведены на паузу для проверки)")
                    else:
                        state.add_log("🛑 Отказ переключения на " + t_mode.upper() + ": " + str(sw.get("retMsg")))
                    resp = sw
            elif path == "/api/x_news":
                if "enabled" in data:
                    state.x_radar.enabled = bool(data.get("enabled", True))
                if "block_on_panic" in data:
                    state.x_radar.block_on_panic = bool(data.get("block_on_panic", True))
                news_res = state.x_radar.refresh_feed(force=True)
                state.add_log("🐦 Радар X (Twitter) & Новостей обновлен: " + str(news_res.get("global_label")))
                resp = {"ok": True, "x_news": news_res}
            elif path == "/api/ml_calibrate":
                if data.get("reset"):
                    state.ml_calibrator.reset_calibration(float(data.get("base_threshold_pct", 58.0)))
                    state.add_log("🧠 ML-калибратор сброшен к базовым весам (1.0x).")
                elif "enabled" in data:
                    state.ml_calibrator.set_enabled(bool(data.get("enabled", True)), float(data.get("base_threshold_pct", state.core_min_prob_pct)))
                    state.add_log("🧠 Режим автокалибровки ML по Closed PnL: " + ("ВКЛ" if data.get("enabled") else "ВЫКЛ"))
                cal_res = state.ml_calibrator.calibrate_from_closed_pnl(state.bybit.get_closed_pnl())
                state.core_min_prob_pct = float(cal_res.get("recommended_ml_threshold_pct", state.core_min_prob_pct))
                state.save_persisted_state()
                state.add_log("🧠 Выполнена перекалибровка весов стратегий по Closed PnL (порог ML: " + str(state.core_min_prob_pct) + "%).")
                resp = {"ok": True, "calibration": cal_res}
            elif path == "/api/scan":
                with state._command_lock:
                    if state._manual_future and not state._manual_future.done():raise ValueError("Скан уже выполняется")
                    task_id=uuid.uuid4().hex
                    state.manual_task={'id':task_id,'status':'queued','error':''}
                    def execute_scan():
                        state.manual_task.update(status='running')
                        try:
                            state.run_scan(trade_booster=bool(data.get('trade_booster',False)),trade_core=bool(data.get('trade_core',False)))
                            state.manual_task.update(status='completed')
                        except Exception as exc:
                            state.manual_task.update(status='failed',error=str(exc)[:250])
                    state._manual_future=state._scan_executor.submit(execute_scan)
                    resp={'ok':True,'status':'queued','task_id':task_id,'retMsg':'Скан поставлен в очередь; результат появится после выполнения'}
            elif path == "/api/strategy_control":
                mode = data.get("mode", "")
                if "interval_sec" in data:
                    state.autopilot_interval_sec = max(30, min(3600, int(data.get("interval_sec", 300) or 300)))
                if "core_max_positions" in data:
                    state.core_max_positions = int(data.get("core_max_positions", 3) or 3)
                if "core_min_prob_pct" in data:
                    state.core_min_prob_pct = float(data.get("core_min_prob_pct", 58.0) or 58.0)

                if mode in ("stop_all","emergency_stop"):state.entry_paused.set()
                elif mode in ("enable_both","toggle_booster","toggle_core"):state.entry_paused.clear()
                if mode == "toggle_booster":
                    state.autopilot_booster_enabled = not state.autopilot_booster_enabled
                    if state.autopilot_booster_enabled:
                        state._last_auto_ts = 0.0
                    state.add_log("🚀 Автопилот Разгона ($1k→$10k): " + ("ВКЛЮЧЕН" if state.autopilot_booster_enabled else "ВЫКЛЮЧЕН"))
                elif mode == "toggle_core":
                    state.autopilot_core_enabled = not state.autopilot_core_enabled
                    if state.autopilot_core_enabled:
                        state._last_auto_ts = 0.0
                    state.add_log("🎯 Автопилот Основной ИИ-стратегии: " + ("ВКЛЮЧЕН" if state.autopilot_core_enabled else "ВЫКЛЮЧЕН"))
                elif mode == "enable_both":
                    state.autopilot_booster_enabled = True
                    state.autopilot_core_enabled = True
                    state._last_auto_ts = 0.0
                    state.add_log("⚡ Включены ОБА автопилота: Разгон ($1k→$10k) + Основная ИИ-стратегия!")
                elif mode == "stop_all":
                    state.autopilot_booster_enabled = False
                    state.autopilot_core_enabled = False
                    state.add_log("⏸ ВСЕ автопилоты остановлены пользователем. Открытые позиции под защитой Smart Guard.")
                elif mode == "emergency_stop":
                    state.autopilot_booster_enabled = False
                    state.autopilot_core_enabled = False
                    state.save_persisted_state()
                    cancel=state.bybit.cancel_all_orders()
                    close=state.bybit.close_all_positions()
                    state.add_log("🚨 АВАРИЙНЫЙ СТОП: автопилоты выключены; запросы отмены и закрытия отправлены. Проверяйте остаток позиций и ордеров.")

                state.save_persisted_state()
                resp = {
                    "ok": True,
                    "booster_enabled": state.autopilot_booster_enabled,
                    "core_enabled": state.autopilot_core_enabled,
                }
            elif path == "/api/booster_config":
                if data.get("reset"):
                    state.booster.reset_challenge(float(data.get("start_equity", 1000.0)), float(data.get("target_equity", 10000.0)))
                    state.bybit.reconcile_and_clear_unresolved()
                    if state.autopilot_booster_enabled or state.autopilot_core_enabled:
                        state.entry_paused.clear()
                    state.refresh_booster_snapshot()
                    state.add_log("🚀 ПОЛНЫЙ СБРОС РАЗГОНА: Баланс $1,000.00, дневной стоп-кран и все кулдауны монет полностью очищены!")
                elif data.get("unlock_limits"):
                    state.booster.unlock_limits_only()
                    state.bybit.reconcile_and_clear_unresolved()
                    if state.autopilot_booster_enabled or state.autopilot_core_enabled:
                        state.entry_paused.clear()
                    state.refresh_booster_snapshot()
                    state.add_log("🔓 Дневной стоп-кран (-3%), счётчик дневных входов и кулдауны монет сняты! Торговля продолжается с текущим балансом.")
                elif "training_mode" in data:
                    t_on = not bool(state.booster.state.get("training_unlimited_mode", False))
                    state.booster.toggle_training_mode(t_on)
                    if t_on:
                        state.bybit.reconcile_and_clear_unresolved()
                        if state.autopilot_booster_enabled or state.autopilot_core_enabled:
                            state.entry_paused.clear()
                    state.refresh_booster_snapshot()
                    state.add_log("🧪 Режим обучения ИИ на Demo (без стоп-крана дня и кулдаунов): " + ("ВКЛЮЧЕН (24/7 без пауз)" if t_on else "ВЫКЛЮЧЕН (штатная защита -3%)"))
                else:
                    state.booster.update_config(
                        bool(data.get("enabled", True)),
                        bool(data.get("auto_pyramid", True)),
                        float(data.get("daily_loss_limit_pct", 3.0)),
                    )
                    state.refresh_booster_snapshot()
                    state.add_log("🚀 Настройки Deposit Booster сохранены.")
            elif path == "/api/booster_trade":
                sym = data["symbol"]
                side = data["side"]
                entry_px = float(data.get("entry_price", 0) or 0)
                tp_px = float(data.get("take_profit", 0) or 0)
                sl_px = float(data.get("stop_loss", 0) or 0)
                b_metrics = state.booster.compute_metrics(state.bybit.get_closed_pnl(), state.bybit.get_positions())
                sizing = state.booster.calculate_order_qty(entry_px, sl_px, b_metrics)
                resp = state.bybit.place_order(
                    sym, side, "Market", sizing["qty_coins"], entry_px, tp_px, sl_px, sizing["leverage"], state.split_tp_enabled, strategy_id="BOOSTER:MANUAL"
                )
                if resp.get("ok"):
                    state.mark_position_source(sym, "🚀 Booster A+")
                    state.booster.register_booster_symbol(sym)
                    state.add_log("🚀 Booster-сделка [" + str(resp.get("exec_mode", "MAKER")) + "]: " + sym + " " + side + " qty=" + str(resp.get("qty")) + " (Риск $" + str(sizing["risk_usd"]) + ")")
            elif path == "/api/pos_pyramid":
                resp = state.bybit.pyramid_position(
                    data["symbol"], data["side"], float(data.get("size", 0) or 0),
                    float(data.get("avg_price", 0) or 0), float(data.get("mark_price", 0) or 0),
                    int(data.get("position_idx", 0) or 0)
                )
                if resp.get("ok"):
                    state.add_log("➕ Пирамидинг +50% выполнен для " + str(data["symbol"]) + ", стоп в безубытке: " + str(resp.get("new_be_sl")))
            elif path == "/api/smart_guard":
                state.smart_guard_enabled = bool(data.get("enabled", True))
                state.split_tp_enabled = bool(data.get("split_tp_enabled", True))
                if "limit_chaser_enabled" in data:
                    state.limit_chaser.enabled = bool(data.get("limit_chaser_enabled", True))
                if "limit_chaser_strict" in data:
                    state.limit_chaser.strict_post_only = bool(data.get("limit_chaser_strict", False))
                state.bybit.spm.breakeven_trigger_pct = float(data.get("breakeven_trigger_pct", 0.55) or 0.55)
                state.bybit.spm.trailing_step_pct = float(data.get("trailing_step_pct", 0.40) or 0.40)
                if "max_leverage" in data:
                    state.max_leverage = max(1,min(3,int(data.get("max_leverage", 3) or 3)))
                if "core_max_positions" in data:
                    state.core_max_positions = int(data.get("core_max_positions", 3) or 3)
                if "core_min_prob_pct" in data:
                    state.core_min_prob_pct = float(data.get("core_min_prob_pct", 58.0) or 58.0)
                if "btc_guard_enabled" in data:
                    state.regime_guard.enabled = bool(data.get("btc_guard_enabled", True))
                if "max_same_sector" in data:
                    state.regime_guard.max_same_sector = int(data.get("max_same_sector", 1) or 1)
                if "max_same_direction_total" in data:
                    state.regime_guard.max_same_direction_total = int(data.get("max_same_direction_total", 2) or 2)
                if "time_stop_enabled" in data:
                    state.regime_guard.time_stop_enabled = bool(data.get("time_stop_enabled", True))
                if "time_stop_minutes" in data:
                    state.regime_guard.time_stop_minutes = int(data.get("time_stop_minutes", 45) or 45)
                if "sound_alerts_enabled" in data:
                    state.sound_alerts_enabled = bool(data.get("sound_alerts_enabled", True))
                if "manual_prefs" in data and isinstance(data["manual_prefs"], dict):
                    state.manual_prefs.update(data["manual_prefs"])
                state.save_persisted_state()
                state.add_log("💾 Все настройки Smart Guard, BTC Regime Guard и Time-Stop сохранены на сервер.")
            elif path == "/api/place_order":
                sym = data["symbol"]
                resp = state.bybit.place_order(
                    sym, data["side"], data.get("order_type", "Market"),
                    float(data.get("qty", 0.02) or 0.02), float(data.get("price", 0) or 0),
                    float(data.get("take_profit", 0) or 0), float(data.get("stop_loss", 0) or 0),
                    int(data.get("leverage", state.max_leverage) or state.max_leverage), state.split_tp_enabled, strategy_id="CORE:MANUAL"
                )
                if resp.get("ok"):
                    state.mark_position_source(sym, "🎯 ML Core")
                    state.add_log("🎯 Открыта сделка ML Core: " + sym + " " + data["side"] + " qty=" + str(resp.get("qty")))
            elif path == "/api/place_manual":
                sym = data.get("symbol", "BTCUSDT")
                side = data.get("side", "Buy")
                otype = data.get("order_type", "Market")
                qty_v = float(data.get("qty", 0.02) or 0.02)
                tp_pct = float(data.get("tp_pct", 0.016) or 0.016)
                sl_pct = float(data.get("sl_pct", 0.008) or 0.008)
                lev_v = int(data.get("leverage", state.max_leverage) or state.max_leverage)
                if not 1<=lev_v<=3:raise ValueError("Плечо ограничено 3x")
                state.max_leverage = lev_v
                state.manual_prefs = {"symbol": sym, "order_type": otype, "qty": qty_v, "tp_pct": round(tp_pct * 100.0, 2), "sl_pct": round(sl_pct * 100.0, 2)}
                state.save_persisted_state()
                tickers = [t for t in state.engine.client.get_tickers() if t.get("symbol") == sym]
                if not tickers:raise ValueError('Нет свежей цены инструмента')
                px = float(data.get('price') or 0) if otype=='Limit' else float(tickers[0]['lastPrice'])
                if px<=0:raise ValueError('Укажите положительную цену лимитной заявки')
                tp = px * (1.0 + tp_pct) if side == "Buy" else px * (1.0 - tp_pct)
                sl = px * (1.0 - sl_pct) if side == "Buy" else px * (1.0 + sl_pct)
                resp = state.bybit.place_order(sym, side, otype, qty_v, px, tp, sl, lev_v, state.split_tp_enabled)
                if resp.get("ok"):
                    state.mark_position_source(sym, "🖐 Ручной")
                    state.add_log("🖐 Открыт ручной ордер: " + sym + " " + side + " qty=" + str(resp.get("qty")))
            elif path == "/api/pos_split_tp":
                avg_px = float(data.get("avg_price", 0) or 0)
                cur_tp = float(data.get("take_profit", 0) or 0)
                tp1_px = avg_px + 0.5 * (cur_tp - avg_px) if cur_tp > 0 else (avg_px * 1.008 if data["side"] == "Buy" else avg_px * 0.992)
                resp = state.bybit.spm.place_tp1_half_limit(data["symbol"], data["side"], float(data.get("size", 0) or 0), tp1_px, int(data.get("position_idx", 0) or 0))
                state.add_log("🎯 Выставлен TP1 50% для " + str(data["symbol"]) + " @ " + str(resp.get("tp1_price")))
            elif path == "/api/pos_close_half":
                resp = state.bybit.close_half_position(data["symbol"], data["side"], str(data["size"]), int(data.get("position_idx", 0) or 0))
                state.add_log("✂️ Отправлена заявка на закрытие 50% позиции " + str(data["symbol"]))
            elif path == "/api/pos_breakeven":
                resp = state.bybit.spm.move_sl_to_breakeven(data["symbol"], data["side"], float(data.get("avg_price", 0) or 0), int(data.get("position_idx", 0) or 0))
                state.add_log("🛡 Стоп-лосс " + str(data["symbol"]) + " переведен в Безубыток (" + str(resp.get("be_price")) + ")")
            elif path == "/api/pos_trailing":
                resp = state.bybit.spm.activate_trailing_stop(data["symbol"], float(data.get("mark_price", 0) or 0), state.bybit.spm.trailing_step_pct, int(data.get("position_idx", 0) or 0))
                state.add_log("📈 Включен Trailing Stop (" + str(resp.get("trailing_dist")) + ") для " + str(data["symbol"]))
            elif path == "/api/close_position":
                resp = state.bybit.close_position(data["symbol"], data["side"], str(data["size"]), int(data.get("position_idx", 0) or 0))
                state.add_log("✖ Отправлена заявка на закрытие позиции " + str(data["symbol"]))
            elif path == "/api/close_all_positions":
                resp = state.bybit.close_all_positions()
                state.add_log("🚨 Отправлены заявки на закрытие позиций; исполнение проверяется по данным биржи.")
            elif path == "/api/cancel_order":
                resp = state.bybit.cancel_order(data["symbol"], data["order_id"])
            elif path == "/api/cancel_all_orders":
                resp = state.bybit.cancel_all_orders()
                state.add_log("🗑 Запрос отмены активных ордеров отправлен.")

            else:
                start_response("404 Not Found",headers+[("Content-Type","application/json")]);return [b'{"ok":false,"retMsg":"Unknown command"}']

            if not resp.get("ok",True):state.add_log("Команда отклонена: " + str(resp.get("retMsg")))
            payload = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            start_response("200 OK", headers + [("Content-Type", "application/json; charset=utf-8")])
            return [payload]
        start_response("404 Not Found", headers + [("Content-Type", "text/plain")])
        return [b"Not Found"]
    def guarded(environ,start_response):
        try:
            return app(environ,start_response)
        except Exception as exc:
            state.add_log("Ошибка HTTP-команды: " + str(exc)[:200])
            start_response("400 Bad Request",[("Content-Type","application/json; charset=utf-8"),("Cache-Control","no-store")])
            return [json.dumps({"ok":False,"retMsg":str(exc)[:200]},ensure_ascii=False).encode()]
    return guarded


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def run_control_center_server(port: int = 8765, open_browser: bool = False) -> None:
    state = ControlCenterState()
    app = create_wsgi_app(state)
    with make_server("127.0.0.1", port, app, server_class=ThreadedWSGIServer, handler_class=QuietWSGIHandler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    run_control_center_server()

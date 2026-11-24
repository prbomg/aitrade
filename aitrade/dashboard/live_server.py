# -*- coding: utf-8 -*-
from __future__ import annotations
from aitrade.storage import atomic_json
import base64, hashlib, hmac, json, os, secrets, subprocess, threading, time, math, uuid, copy, traceback
from http.cookies import SimpleCookie
from aitrade.storage import Ledger
from aitrade.exchange.transport import ExchangeError
from concurrent.futures import ThreadPoolExecutor
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer
from urllib.parse import parse_qs, quote, urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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
from aitrade.execution.audit_metrics import StrategyFunnel24h, compute_live_strategy_metrics
from aitrade.execution.shadow_engine import LiveShadowEngine
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.execution.limit_chaser import SmartLimitChaser


STATE_FILE = BASE_DIR / "data" / "ui_state.json"
DEFAULT_RELEASE_VERSION = "2026.11.24-intraday.45"


def is_protective_order(order: Dict[str, Any]) -> bool:
    stop_t = str(order.get("stopOrderType") or "").strip()
    return bool(order.get("reduceOnly")) or (bool(stop_t) and stop_t.lower() != "unknown")


def compute_portfolio_risk_usd(
    positions: List[Dict[str, Any]],
    open_orders: List[Dict[str, Any]],
    strict: bool = False,
) -> float:
    """Единый расчёт совокупного риска портфеля (открытые позиции + активные входные заявки) для панели и исполнителя."""
    total_risk = 0.0
    for p in positions or []:
        ep = float(p.get("avgPrice") or 0)
        psl = float(p.get("stopLoss") or 0)
        sz = float(p.get("size") or 0)
        if psl <= 0:
            if strict:
                raise ValueError("Открытая позиция без SL")
            continue
        if ep > 0 and sz > 0:
            total_risk += sz * (abs(ep - psl) + ep * 0.0054)
    for o in open_orders or []:
        if is_protective_order(o):
            continue
        ep = float(o.get("price") or 0)
        osl = float(o.get("stopLoss") or 0)
        qty = float(o.get("leavesQty") or o.get("qty") or 0)
        if not ep or not osl:
            if strict:
                raise ValueError("Непроверенный риск активной заявки")
            continue
        if qty > 0:
            total_risk += qty * (abs(ep - osl) + ep * 0.0054)
    return total_risk


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

    def apply_credentials(self, api_key: str, api_secret: str, base_url: str, sync_engine_client: bool = True) -> None:
        self.base_url = base_url
        if sync_engine_client:
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

    UNCERTAIN_STALE_TTL_SEC = 1800.0

    def _exchange_has_no_open_state(self, symbol: str) -> bool:
        try:
            open_orders = self.spm.paged_get('/v5/order/realtime', {'category': 'linear', 'symbol': symbol})
            if open_orders:
                return False
            pos_rows = self.spm.paged_get('/v5/position/list', {'category': 'linear', 'symbol': symbol})
            if any(float(p.get('size') or 0) > 0 for p in pos_rows):
                return False
            return True
        except Exception:
            return False

    def _reconcile_intent(self, intent_id, symbol, stored, created=None):
        is_stale = created is not None and (time.time() - float(created)) >= self.UNCERTAIN_STALE_TTL_SEC
        children=self.ledger.orders_for(intent_id)
        if not children:
            # Pre-journal ambiguous chaser errors cannot be cleared by querying only a parent.
            if stored.get('error') and not stored.get('orderIds') and not stored.get('orderId'):
                if is_stale and self._exchange_has_no_open_state(symbol):
                    self.ledger.finish(intent_id, 'REJECTED', {'reconcile_note': 'stale_uncertain_auto_cleared_no_exchange_state'})
                    self.ledger.event('UNCERTAIN_STALE_CLEARED', {'intent_id': intent_id, 'symbol': symbol})
                return
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
        if missing and is_stale and total == 0 and self._exchange_has_no_open_state(symbol):
            self.ledger.finish(intent_id, 'REJECTED', {'confirmedQty': 0.0, 'orderIds': ids, 'reconcile_note': 'stale_missing_order_auto_cleared'})
            self.ledger.event('UNCERTAIN_STALE_CLEARED', {'intent_id': intent_id, 'symbol': symbol})
            return
        status='UNCERTAIN' if missing else ('PENDING' if not all_terminal else ('FILLED' if total>=float(stored.get('requestedQty',stored.get('qty',0)) or 0)-1e-10 and total>0 else ('PARTIAL' if total>0 else 'REJECTED')))
        self.ledger.finish(intent_id,status,{'confirmedQty':total,'orderIds':ids})

    def reconcile_pending(self):
        if not self._entry_lock.acquire(blocking=False):return
        try:
            with self.ledger.connect() as c:
                rows=c.execute("SELECT id,symbol,payload,created FROM intents WHERE environment=? AND status IN ('SUBMITTING','UNCERTAIN','PENDING')",(self.ledger.environment,)).fetchall()
            for iid,symbol,raw,created in rows:self._reconcile_intent(iid,symbol,json.loads(raw),created=created)
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

    def get_closed_pnl(self, reconcile: bool = True):
        if reconcile:
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

    def close_position(self, symbol: str, side: str, size: str, position_idx: int = 0, lock_timeout: Optional[float] = None) -> Dict[str, Any]:
        if lock_timeout is None:
            with self._entry_lock:
                return self._close_position_locked(symbol,side,size,position_idx)
        acquired = self._entry_lock.acquire(timeout=float(lock_timeout))
        try:
            return self._close_position_locked(symbol,side,size,position_idx)
        finally:
            if acquired:
                self._entry_lock.release()

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
            booster_obj=getattr(owner,'booster',None) if owner else None
            b_state=getattr(booster_obj,'state',None)
            is_isolated_booster=bool(strategy_id.startswith('BOOSTER') and isinstance(b_state,dict) and b_state.get('isolated_subaccount'))
            max_lev_allowed=5 if is_isolated_booster else 3
            if not 1<=int(leverage)<=max_lev_allowed:raise ValueError(f'Плечо ограничено {max_lev_allowed}x')
            self.reconcile_pending()
            if self.ledger.unresolved(symbol):raise ValueError('Есть неподтверждённая заявка; требуется сверка')
            positions=self.get_positions();orders=[o for o in self.get_open_orders() if not o.get('reduceOnly')]
            busy={p['symbol'] for p in positions}|{o['symbol'] for o in orders}
            demo_lab_cfg=self.engine.settings.get('demo_lab',{}) if isinstance(self.engine.settings,dict) else {}
            demo_lab_on=bool(demo_lab_cfg.get('enabled',False) and self.base_url=='https://api-demo.bybit.com')
            is_t1_core='T1_DONCHIAN_TSMOM' in str(strategy_id)
            max_busy=int(demo_lab_cfg.get('max_concurrent_positions',8) or 8) if demo_lab_on else (2 if is_isolated_booster else (4 if is_t1_core else 3))
            if symbol in busy or len(busy)>=max_busy:raise ValueError('Лимит позиций/заявок или повторный символ')
            midnight=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000
            if isinstance(b_state,dict):
                reset_ms=max(int(b_state.get('limits_reset_at_ms',0) or 0),int(b_state.get('started_at_ms',0) or 0))
                cutoff_ms=max(midnight,float(reset_ms))
                training_mode=bool(b_state.get('training_unlimited_mode',False))
            else:
                cutoff_ms=midnight
                training_mode=False
            if demo_lab_on and bool(demo_lab_cfg.get('disable_daily_stops_on_demo',False)):
                training_mode=True
            max_daily=int(self.engine.settings.get('risk',{}).get('max_daily_trades',10) or 10)
            if not training_mode and self.ledger.today_count(since_ts=cutoff_ms/1000.0)>=max_daily:raise ValueError(f'Лимит {max_daily} входов в день')
            acct_bal=self.get_account_summary()['usdt_balance']
            capital=acct_bal if is_isolated_booster else min(1000.,acct_bal)
            b_metrics_live=None
            if strategy_id.startswith('BOOSTER') and booster_obj:
                b_metrics_live=booster_obj.compute_metrics(self.get_closed_pnl(),positions)
                if b_metrics_live.get('goal_reached'):raise ValueError('Цель кампании достигнута; новые входы остановлены')
                capital=min(capital,b_metrics_live['risk_capital_usd'])
            if capital<=0:raise ValueError('Нет подтверждённого капитала')
            daily=sum(float(c['closedPnl']) for c in self.get_closed_pnl() if int(c['updatedTime'])>=cutoff_ms)
            upnl=sum(float(p.get('unrealisedPnl') or 0) for p in positions)
            daily_cap_frac=0.08 if is_isolated_booster else 0.03
            upnl_cap_frac=0.04 if is_isolated_booster else 0.01
            if not training_mode and upnl<=-capital*upnl_cap_frac:raise ValueError(f'Плавающий убыток > {int(upnl_cap_frac*100)}%: новые входы заблокированы')
            if not training_mode and daily+min(upnl,0)<=-capital*daily_cap_frac:raise ValueError(f'Дневной лимит -{int(daily_cap_frac*100)}%: новые входы заблокированы')
            if owner:
                bbo_age=30 if (is_t1_core or demo_lab_on) else 15
                try:
                    bid,ask=owner.ws_hub.get_bbo(symbol,bbo_age)
                except Exception:
                    if not (is_t1_core or demo_lab_on):raise
                    ob_rest=self.engine.client.get_orderbook(symbol,limit=5)
                    if not ob_rest.best_bid or not ob_rest.best_ask:raise ValueError('Нет свежего стакана '+symbol)
                    bid,ask=ob_rest.best_bid,ob_rest.best_ask
                ref=ask if side=='Buy' else bid
                spread_bps=((ask-bid)/((ask+bid)*0.5))*10000.0 if (bid>0 and ask>0) else 0.0
                max_spread=8.0 if symbol in ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT') else 20.0
                if spread_bps>max_spread:raise ValueError(f'Спред {spread_bps:.1f} bps > {max_spread:.0f} bps')
                fr_map=getattr(owner,'funding_rates',None)
                if (not isinstance(fr_map,dict) or symbol not in fr_map or time.time()-getattr(owner,'funding_updated_at',0)>600) and hasattr(owner,'refresh_funding_rates'):
                    owner.refresh_funding_rates()
                    fr_map=getattr(owner,'funding_rates',None)
                if not isinstance(fr_map,dict) or symbol not in fr_map or time.time()-getattr(owner,'funding_updated_at',0)>600:raise ValueError('Нет свежего фандинга: обновите скан')
                fr_val=float(fr_map[symbol])
                if (side=='Buy' and fr_val>0.0005) or (side=='Sell' and fr_val<-0.0005):
                    raise ValueError('Экстремальный фандинг против стороны входа > 0.05%')
                ws_info=owner.ws_hub.get_summary()
                ws_warm=bool(ws_info.get('flow_window_complete',False)) or training_mode or (time.time()-getattr(owner,'started_at',0)>=120 and int(ws_info.get('symbols_live',0) or 0)>0)
                if strategy_id.startswith('BOOSTER') and not ws_warm:raise ValueError('Поток CVD/ликвидаций прогревается после подключения (2 мин)')
                try:
                    permitted,reason=owner.regime_guard.can_open_position(symbol,side,positions,owner.ws_hub.get_summary(),strategy_id=strategy_id)
                except TypeError:
                    permitted,reason=owner.regime_guard.can_open_position(symbol,side,positions,owner.ws_hub.get_summary())
                if not permitted:raise ValueError(reason)
            else:raise ValueError('Нет подтвержденного потока рынка')
            max_dev=0.05 if order_type=='Limit' else (0.012 if (is_t1_core or demo_lab_on) else 0.003)
            if price and abs(float(price)/ref-1)>max_dev:raise ValueError('Сигнал устарел: цена изменилась')
            ref=float(price) if order_type=='Limit' and price else ref
            if not (sl<ref<tp if side=='Buy' else tp<ref<sl):raise ValueError('Неверное расположение SL/TP')
            min_sl_req=0.0080 if 'S3_VWAP_RANGE' in str(strategy_id) else 0.0055
            if abs(ref-sl)/ref<min_sl_req:raise ValueError(f'Стоп-лосс уже {min_sl_req*100:.2f}%: издержки съедают R:R')
            min_rr_req=1.15 if 'S3_VWAP_RANGE' in str(strategy_id) else 1.5
            if abs(tp-ref)/abs(ref-sl)<min_rr_req:raise ValueError(f'R:R ниже {min_rr_req}')
            spec=self.spm.get_spec(symbol)
            sl=float(self.spm.fmt_step(sl,spec['tick_size'],False));tp=float(self.spm.fmt_step(tp,spec['tick_size'],False))
            if not (sl<ref<tp if side=='Buy' else tp<ref<sl):raise ValueError('SL/TP после округления невалидны')
            risk_per_coin=abs(ref-sl)+ref*(.0011+.0015+.0028)
            c_w=float((signal_context or {}).get('cal_weight',1.0) or 1.0)
            if is_isolated_booster and b_metrics_live:
                risk_frac=max(0.01,min(0.045,(float(b_metrics_live.get('effective_risk_pct',4.0))/100.0)*min(1.0,c_w)))
            elif strategy_id.startswith('CORE'):
                base_core_r=0.015 if (is_t1_core or demo_lab_on) else float(self.engine.settings.get('risk',{}).get('risk_per_trade_pct',0.005) or 0.005)
                vol_scale=max(0.35,min(1.80,float(((signal_context or {}).get('features') or {}).get('vol_scale',1.0) or 1.0))) if is_t1_core else 1.0
                dd_mult=1.0
                if owner and hasattr(owner,'check_autotrade_drawdown'):
                    dd_mult,dd_stop=owner.check_autotrade_drawdown(acct_bal)
                    if dd_stop and not training_mode:raise ValueError('Просадка Автотрейдинга >= 15% от пика: новые входы остановлены')
                max_r_cap=0.027 if is_t1_core else (0.018 if demo_lab_on else 0.010)
                risk_frac=max(0.0035,min(max_r_cap,base_core_r*vol_scale*c_w*dd_mult))
            else:
                risk_frac=0.015 if (demo_lab_on and strategy_id.startswith('BOOSTER')) else (max(0.0035,min(0.0070,0.005*c_w)) if strategy_id.startswith('BOOSTER') else 0.005)
            maxqty=capital*risk_frac/risk_per_coin
            # В Demo Lab каждая стратегия рассчитывается от своих $1,000 (capital), но портфельный потолок использует баланс Demo до $50,000
            portfolio_cap_usd=min(float(demo_lab_cfg.get('portfolio_cap_usdt',50000.0) or 50000.0),acct_bal) if demo_lab_on else capital
            used_notional=sum(float(p['size'])*float(p.get('markPrice') or p['avgPrice']) for p in positions)+sum(float(o.get('leavesQty') or o['qty'])*float(o.get('price') or 0) for o in orders)
            notional_mult=5.0 if is_isolated_booster else 3.0
            available_notional=max(0.,portfolio_cap_usd*notional_mult-used_notional)
            qty=float(self.spm.fmt_step(min(float(qty),maxqty,available_notional/ref,spec['max_qty']),spec['qty_step'],True))
            if qty<spec['min_qty'] or qty*ref<max(spec['min_notional'],24):raise ValueError('Размер после ограничения риска ниже минимума')
            totalrisk = qty * risk_per_coin + compute_portfolio_risk_usd(positions, orders, strict=True)
            total_risk_cap=0.25 if demo_lab_on else (0.07 if is_isolated_booster else (0.06 if is_t1_core else 0.02))
            if totalrisk>portfolio_cap_usd*total_risk_cap:raise ValueError(f'Совокупный риск выше {int(total_risk_cap*100)}%')
            if owner and (strategy_id.startswith('BOOSTER') or strategy_id.startswith('CORE')) and owner.entry_paused.is_set():raise ValueError('Вход остановлен пользователем')
            intent_id='at-'+uuid.uuid4().hex[:30]
            hold_min=14400 if is_t1_core else (10080 if 'T3_KELTNER_TREND_4H' in str(strategy_id) else (2880 if 'T2_RS_BTC_BREAKOUT' in str(strategy_id) else (2160 if 'C2_MTF_COMBO_BREAKOUT' in str(strategy_id) else (1440 if 'C3_ENSEMBLE_VOTE_2OF3' in str(strategy_id) else (1080 if any(k in str(strategy_id) for k in ('S1_TREND_RETEST','B1_SQUEEZE_BREAKOUT','B2_DONCHIAN_INTRADAY','B7_FUNDING_SQUEEZE_MOMENTUM')) else (720 if any(k in str(strategy_id) for k in ('B5_OI_TREND_EXPANSION','B6_EMA_PULLBACK_TREND')) else (480 if any(k in str(strategy_id) for k in ('AUTOTRADE_MOMENTUM_FUNDING','B3_VWAP_MOMENTUM_IMPULSE','B4_ORB_SESSION_BREAKOUT')) else (240 if 'S3_VWAP_RANGE' in str(strategy_id) else (180 if strategy_id.startswith('BOOSTER') else 0)))))))))
            _rm=getattr(owner,'release_manifest',None)
            rel_ver=_rm.get('version',DEFAULT_RELEASE_VERSION) if isinstance(_rm,dict) else DEFAULT_RELEASE_VERSION
            self.ledger.intent(intent_id,symbol,strategy_id,qty*risk_per_coin,{'side':side,'qty':qty,'requestedQty':qty,'campaign_id':str(b_state.get('started_at_ms',0)) if isinstance(b_state,dict) and strategy_id.startswith('BOOSTER') else None,'strategy_version':rel_ver,'signal_context':signal_context or {},'entry':ref,'sl':sl,'tp':tp,'exit_plan':{'version':1,'max_hold_minutes':hold_min}})
            if hasattr(self.spm,'ensure_isolated_margin') and 'FUNDING_CARRY' not in str(strategy_id):
                iso_res=self.spm.ensure_isolated_margin(symbol,int(leverage))
                if isinstance(iso_res,dict) and iso_res.get('ok') is False:
                    raise ValueError('Не удалось подтвердить режим Isolated Margin: '+str(iso_res.get('retMsg','')))
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
                want_post_only=(order_type=='Limit') and (bool(((signal_context or {}).get('features') or {}).get('post_only')) or any(k in str(strategy_id) for k in ('S1_TREND_RETEST','S3_VWAP_RANGE','B1_SQUEEZE_BREAKOUT','AUTOTRADE_MOMENTUM_FUNDING')))
                tif='PostOnly' if want_post_only else ('GTC' if order_type=='Limit' else 'IOC')
                payload={'category':'linear','symbol':symbol,'side':side,'qty':str(qty),'orderType':order_type,'positionIdx':idx,'orderLinkId':intent_id,'timeInForce':tif,'stopLoss':self.spm.fmt_step(sl,spec['tick_size'],False),'takeProfit':self.spm.fmt_step(tp,spec['tick_size'],False),'tpslMode':'Full'}
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
        self.booster_ledger = Ledger(BASE_DIR / "data" / "execution_ledger_booster.sqlite3")
        self.autotrade_ledger = Ledger(BASE_DIR / "data" / "execution_ledger_autotrade.sqlite3")
        self.oi_scanner = OISqueezeScanner()
        self._oi_scan_ts: float = 0.0
        self._oi_scan_cache: List[Dict[str, Any]] = []
        self.strategy_funnel = StrategyFunnel24h(BASE_DIR / "data" / "strategy_funnel_24h.json")
        self.shadow_engine = LiveShadowEngine(BASE_DIR / "data" / "shadow_lab.sqlite3")
        self._live_metrics_cache: Dict[str, Any] = {}
        self._live_metrics_ts: float = 0.0
        self.regime_guard = RegimeAndCorrelationGuard()
        self.regime_guard.demo_lab_enabled = bool((self.settings.get("demo_lab") or {}).get("enabled", False) and self.bybit.base_url == "https://api-demo.bybit.com")
        self.ml_calibrator = AdaptiveMLCalibrator(BASE_DIR / "data")
        self.x_radar = XNewsSentimentRadar()
        self.env_manager = BybitEnvManager(BASE_DIR / "data", self.engine.client.api_key, self.engine.client.api_secret)
        b_creds = self.env_manager.get_account_credentials("booster")
        a_creds = self.env_manager.get_account_credentials("autotrade")
        self.booster.state["isolated_subaccount"] = bool(b_creds.get("isolated", False))
        active_creds = self.env_manager.get_active_credentials()
        if a_creds.get("isolated") and a_creds.get("api_key"):
            self.bybit.ledger = self.autotrade_ledger
            self.bybit.spm.ledger = self.autotrade_ledger
            self.bybit.spm.guard_path = BASE_DIR / "data" / "position_guard_autotrade.json"
            self.bybit.apply_credentials(a_creds["api_key"], a_creds["api_secret"], a_creds["base_url"])
        elif active_creds.get("api_key"):
            self.bybit.apply_credentials(active_creds["api_key"], active_creds["api_secret"], active_creds["base_url"])
        if b_creds.get("isolated") and b_creds.get("api_key"):
            self.booster_bybit = BybitV5DirectController(self.engine)
            self.booster_bybit.ledger = self.booster_ledger
            self.booster_bybit.spm.ledger = self.booster_ledger
            self.booster_bybit.spm.guard_path = BASE_DIR / "data" / "position_guard_booster.json"
            self.booster_bybit.owner = self
            self.booster_bybit.apply_credentials(b_creds["api_key"], b_creds["api_secret"], b_creds["base_url"], sync_engine_client=False)
            self.booster_bybit.attach_chaser(SmartLimitChaser(self.booster_bybit.spm, self.ws_hub, BASE_DIR / "data"))
        else:
            self.booster_bybit = self.bybit
        self.backtest_cache: Dict[str, Any] = {}
        self.last_scan: Dict[str, Any] = {}

        saved = self._load_persisted_state()
        self.autotrade_hwm = float(saved.get("autotrade_hwm", 0.0) or 0.0)
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
        if self.booster_bybit is not self.bybit:
            self.booster_bybit.spm.breakeven_trigger_pct = self.bybit.spm.breakeven_trigger_pct
            self.booster_bybit.spm.trailing_step_pct = self.bybit.spm.trailing_step_pct
            b_ch = getattr(self.booster_bybit, "chaser", None)
            if b_ch is not None:
                b_ch.enabled = self.limit_chaser.enabled
                b_ch.strict_post_only = self.limit_chaser.strict_post_only
        self.max_leverage = int(saved.get("max_leverage", self.settings.get("risk", {}).get("max_leverage", 3)) or 3)
        self.core_max_positions = int(saved.get("core_max_positions", 4) or 4)
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
        self._logs_lock = threading.Lock()
        self._state_save_lock = threading.RLock()
        self._lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._last_booster_closed: List[Dict[str, Any]] = []
        self._last_booster_positions: List[Dict[str, Any]] = []
        self._snapshot = {}
        self._snapshot_ts = 0.0
        self._snapshot_error = "Первый снимок загружается"
        self._snapshot_lock = threading.Lock()
        self.entry_paused = threading.Event()
        self.entry_pause_reason = ""
        self.entry_paused_at = 0.0
        self._entry_pause_source = ""
        self._guard_consecutive_errors = 0
        self._guard_consecutive_ok = 0
        if not (self.autopilot_core_enabled or self.autopilot_booster_enabled):
            self.entry_paused.set()
            self.entry_pause_reason = "Автопилоты выключены при старте"
            self._entry_pause_source = "user"
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
        lock = getattr(self, "_state_save_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            payload = {
                "autotrade_hwm": self.autotrade_hwm,
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
                "position_sources": dict(self.position_sources),
                "position_opened_ts": dict(self.position_opened_ts),
                "manual_prefs": dict(self.manual_prefs),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            atomic_json(STATE_FILE, payload)
        finally:
            if lock is not None:
                lock.release()

    def mark_position_source(self, symbol: str, source_tag: str) -> None:
        lock = getattr(self, "_state_save_lock", None)
        if lock is not None:
            with lock:
                self.position_sources[symbol] = source_tag
                self.position_opened_ts[symbol] = time.time()
                self.save_persisted_state()
        else:
            self.position_sources[symbol] = source_tag
            self.position_opened_ts[symbol] = time.time()
            self.save_persisted_state()

    def get_position_age_minutes(self, pos: Dict[str, Any]) -> float:
        sym = str(pos.get("symbol", ""))
        now = time.time()
        if pos.get("opened_ms"):return round(max(0.,(now*1000-pos["opened_ms"])/60000),1)
        lock = getattr(self, "_state_save_lock", None)
        if lock is not None:
            lock.acquire()
        try:
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
        finally:
            if lock is not None:
                lock.release()

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
                wfa_val = self._load_wfa_validation_summary()
                self.backtest_cache = {
                    "status": "ok",
                    "source": "wfa_validation.json",
                    "updated_at": wfa_val.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    "strategies": wfa_val.get("strategies", {}),
                }
            except Exception as exc:
                self.backtest_cache = {"status": "error", "note": str(exc)[:200]}
            time.sleep(3600)

    def _get_logs_copy(self) -> List[str]:
        lock = getattr(self, "_logs_lock", None)
        if lock is not None:
            with lock:
                return list(self.logs)
        return list(self.logs)

    def add_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = "[" + ts + "] " + str(msg)
        lock = getattr(self, "_logs_lock", None)
        if lock is not None:
            with lock:
                self.logs.insert(0, entry)
                del self.logs[120:]
        else:
            self.logs.insert(0, entry)
            self.logs = self.logs[:120]
        print("KAIROS " + str(msg), flush=True)
        self.bybit.ledger.event("LOG", {"message": str(msg)[:1000]})

    def _news_loop(self):
        while True:
            try:self.x_radar.refresh_feed()
            except Exception as exc:self.add_log('Ошибка обновления новостей: '+str(exc)[:150])
            time.sleep(60)

    def _active_controllers(self) -> List[BybitV5DirectController]:
        ctrls = [self.bybit]
        b_ctrl = getattr(self, "booster_bybit", None)
        if b_ctrl is not None and b_ctrl is not self.bybit:
            ctrls.append(b_ctrl)
        return ctrls

    def execute_emergency_stop(self) -> Dict[str, Any]:
        """Безопасный аварийный стоп: сначала отменяет только входные ордера, затем закрывает позиции
        по каждому контуру в отдельном try/except, сверяет остаток и сохраняет SL/TP при частичном выходе."""
        self.autopilot_booster_enabled = False
        self.autopilot_core_enabled = False
        self.entry_paused.set()
        self.entry_pause_reason = "Аварийный стоп"
        self.entry_paused_at = time.time()
        self._entry_pause_source = "emergency"
        try:
            self.save_persisted_state()
        except Exception:
            pass

        accounts_report: List[Dict[str, Any]] = []
        any_error = False
        any_partial = False

        for idx, ctrl in enumerate(self._active_controllers()):
            acc_name = "autotrade" if ctrl is self.bybit else f"booster_{idx}"
            acc_rep: Dict[str, Any] = {
                "account": acc_name,
                "ok": True,
                "entry_orders_cancelled": 0,
                "close_submitted": 0,
                "remaining_positions": 0,
                "remaining_orders": 0,
                "remaining_entry_orders": 0,
                "unprotected_positions": [],
                "errors": [],
            }
            try:
                open_orders = ctrl.get_open_orders()
                for o in open_orders:
                    if is_protective_order(o):
                        continue
                    c_res = ctrl.cancel_order(str(o.get("symbol", "")), str(o.get("orderId", "")))
                    if c_res.get("ok"):
                        acc_rep["entry_orders_cancelled"] += 1
                    else:
                        acc_rep["errors"].append(f"Отмена входа {o.get('symbol')}: {c_res.get('retMsg', 'error')}")
            except Exception as exc:
                acc_rep["errors"].append(f"Ошибка отмены входных ордеров: {str(exc)[:140]}")

            try:
                positions = ctrl.get_positions()
                for p in positions:
                    try:
                        try:
                            cl_res = ctrl.close_position(
                                str(p.get("symbol", "")),
                                str(p.get("side", "Buy")),
                                str(p.get("size", "0")),
                                int(p.get("positionIdx", 0) or 0),
                                lock_timeout=3.0,
                            )
                        except TypeError:
                            cl_res = ctrl.close_position(
                                str(p.get("symbol", "")),
                                str(p.get("side", "Buy")),
                                str(p.get("size", "0")),
                                int(p.get("positionIdx", 0) or 0),
                            )
                        if cl_res.get("ok"):
                            acc_rep["close_submitted"] += 1
                        else:
                            acc_rep["errors"].append(f"Закрытие {p.get('symbol')}: {cl_res.get('retMsg', 'error')}")
                    except Exception as p_exc:
                        acc_rep["errors"].append(f"Закрытие {p.get('symbol')}: {str(p_exc)[:120]}")
            except Exception as exc:
                acc_rep["errors"].append(f"Ошибка чтения/закрытия позиций: {str(exc)[:140]}")

            try:
                rem_pos = ctrl.get_positions()
                acc_rep["remaining_positions"] = len(rem_pos)
                if not rem_pos:
                    rem_orders = ctrl.get_open_orders()
                    if rem_orders:
                        for ro in rem_orders:
                            try:
                                fc_res = ctrl.cancel_order(str(ro.get("symbol", "")), str(ro.get("orderId", "")))
                                if not fc_res.get("ok"):
                                    acc_rep["errors"].append(
                                        f"Финальная отмена {ro.get('symbol')}: {fc_res.get('retMsg', 'error')}"
                                    )
                            except Exception as fc_exc:
                                acc_rep["errors"].append(
                                    f"Финальная отмена {ro.get('symbol')}: {str(fc_exc)[:120]}"
                                )
                        verify_orders = ctrl.get_open_orders()
                        acc_rep["remaining_orders"] = len(verify_orders)
                        acc_rep["remaining_entry_orders"] = sum(1 for o in verify_orders if not is_protective_order(o))
                        if verify_orders:
                            any_partial = True
                else:
                    any_partial = True
                    for rp in rem_pos:
                        if float(rp.get("stopLoss") or 0) <= 0:
                            acc_rep["unprotected_positions"].append(str(rp.get("symbol", "")))
                    rem_orders = ctrl.get_open_orders()
                    acc_rep["remaining_orders"] = len(rem_orders)
                    acc_rep["remaining_entry_orders"] = sum(1 for o in rem_orders if not is_protective_order(o))
            except Exception as exc:
                acc_rep["errors"].append(f"Ошибка сверки остатка: {str(exc)[:140]}")

            if acc_rep["errors"] or acc_rep["unprotected_positions"]:
                acc_rep["ok"] = False
                any_error = True
            accounts_report.append(acc_rep)

        status = "ERROR" if any_error else ("PARTIAL" if any_partial else "CONFIRMED")
        ok = (status == "CONFIRMED")
        if status == "CONFIRMED":
            summary_msg = "Аварийный стоп подтверждён: входные ордера отменены, позиции закрыты."
        elif status == "PARTIAL":
            rem_total = sum(a["remaining_positions"] for a in accounts_report)
            summary_msg = f"Частичное исполнение аварийного стопа: осталось позиций {rem_total}, защитные SL/TP сохранены."
        else:
            summary_msg = "Аварийный стоп завершился с ошибками на части счетов — проверьте остаток и защиту."
        self.add_log(f"🚨 [АВАРИЙНЫЙ СТОП · {status}] {summary_msg}")
        return {
            "ok": ok,
            "status": status,
            "booster_enabled": False,
            "core_enabled": False,
            "entry_paused": True,
            "entry_pause_reason": self.entry_pause_reason,
            "accounts": accounts_report,
            "retMsg": summary_msg,
        }

    def check_autotrade_drawdown(self, equity_usd: float) -> Tuple[float, bool]:
        """3-уровневый Kill-Switch Автотрейдинга: -8% от HWM -> риск 0.5x, -15% -> полная остановка."""
        eq = float(equity_usd or 0.0)
        if eq <= 0:
            return 1.0, False
        hwm = max(float(getattr(self, "autotrade_hwm", 0.0) or 0.0), eq)
        self.autotrade_hwm = hwm
        dd_pct = ((hwm - eq) / max(hwm, 1e-9)) * 100.0
        if dd_pct >= 15.0:
            return 0.0, True
        if dd_pct >= 8.0:
            return 0.5, False
        return 1.0, False

    def _check_dead_mans_switch(self) -> int:
        """Dead-man's switch: при потере WS-связи > 30с отменяет все входные лимитные ордера на всех контурах."""
        if time.time() - getattr(self, "started_at", 0.0) < 30.0:
            return 0
        ws_sum = self.ws_hub.get_summary()
        age = float(ws_sum.get("last_msg_age_sec", -1) or -1)
        disconnected = (not bool(ws_sum.get("connected", False))) or (age > 30.0)
        if not disconnected:
            return 0
        cancelled = 0
        for ctrl in self._active_controllers():
            for o in ctrl.get_open_orders():
                if bool(o.get("reduceOnly")):
                    continue
                res = ctrl.cancel_order(str(o.get("symbol", "")), str(o.get("orderId", "")))
                if res.get("ok"):
                    cancelled += 1
        if cancelled > 0:
            self.add_log(f"🚨 [Dead-Man Switch] Потеря WS ({age:.0f}с) — отменено {cancelled} входных лимитных ордеров!")
        return cancelled

    @staticmethod
    def _is_active_market_close_order(o: Dict[str, Any], sym: str) -> bool:
        """Отличает реальную активную Market-заявку на закрытие от условных Untriggered SL/TP ордеров."""
        if o.get("symbol") != sym or not bool(o.get("reduceOnly")) or o.get("orderType") != "Market":
            return False
        stop_type = str(o.get("stopOrderType") or "").strip()
        if stop_type and stop_type.lower() != "unknown":
            return False
        st = str(o.get("orderStatus") or "").strip()
        if st in ("Untriggered", "Deactivated", "Cancelled", "Rejected", "Filled"):
            return False
        return True

    CRITICAL_GUARD_ACTIONS = frozenset({
        "MISSING_STOP_LOSS",
        "UNOWNED_MISSING_STOP_LOSS",
        "EXCHANGE_LIQUIDATION_DISTANCE_LOW",
    })
    GUARD_TRANSIENT_PAUSE_THRESHOLD = 3
    GUARD_RECOVERY_OK_THRESHOLD = 3

    def is_guard_pause_blocking(self) -> Tuple[bool, str]:
        """Возвращает (True, причина), если защитная пауза Smart Guard ещё не готова к ручному снятию."""
        guard_err = str((getattr(self, "guard_status", {}) or {}).get("last_error") or "").strip()
        pause_src = str(getattr(self, "_entry_pause_source", "") or "")
        ok_cycles = int(getattr(self, "_guard_consecutive_ok", 0) or 0)
        req_ok = int(getattr(self, "GUARD_RECOVERY_OK_THRESHOLD", 3) or 3)
        if guard_err:
            return True, f"активна ошибка Smart Guard ({guard_err[:80]})"
        if self.entry_paused.is_set() and pause_src in ("guard_transient", "guard_critical") and ok_cycles < req_ok:
            return True, f"Smart Guard восстанавливается ({ok_cycles}/{req_ok} успешных циклов)"
        return False, ""

    def _handle_guard_cycle_outcome(self, transient_errors: List[str], critical_errors: List[str]) -> None:
        if critical_errors:
            self._guard_consecutive_ok = 0
            self._guard_consecutive_errors = getattr(self, "_guard_consecutive_errors", 0) + 1
            self.guard_status["last_error"] = " | ".join(critical_errors + transient_errors)[:200]
            self.entry_paused.set()
            self.entry_pause_reason = critical_errors[0][:120]
            self.entry_paused_at = time.time()
            self._entry_pause_source = "guard_critical"
            return
        if transient_errors:
            self._guard_consecutive_ok = 0
            self._guard_consecutive_errors = getattr(self, "_guard_consecutive_errors", 0) + 1
            self.guard_status["last_error"] = " | ".join(transient_errors)[:200]
            if self._guard_consecutive_errors >= self.GUARD_TRANSIENT_PAUSE_THRESHOLD:
                cur_src = getattr(self, "_entry_pause_source", "")
                if (not self.entry_paused.is_set()) or cur_src in ("", "guard_transient"):
                    was_paused = self.entry_paused.is_set()
                    self.entry_paused.set()
                    self.entry_pause_reason = transient_errors[0][:120]
                    self.entry_paused_at = time.time()
                    self._entry_pause_source = "guard_transient"
                    if not was_paused:
                        self.add_log(
                            f"⏸ Smart Guard: входы на автопаузе после {self._guard_consecutive_errors} сбоев подряд "
                            f"({self.entry_pause_reason}). Автовосстановление после {self.GUARD_RECOVERY_OK_THRESHOLD} успешных циклов."
                        )
            return
        self._guard_consecutive_errors = 0
        self._guard_consecutive_ok = getattr(self, "_guard_consecutive_ok", 0) + 1
        self.guard_status["last_success"] = time.time()
        pause_src = str(getattr(self, "_entry_pause_source", "") or "")
        guard_pause_active = self.entry_paused.is_set() and pause_src in ("guard_transient", "guard_critical")
        if (not guard_pause_active) or self._guard_consecutive_ok >= self.GUARD_RECOVERY_OK_THRESHOLD:
            self.guard_status["last_error"] = ""
        if (
            self.entry_paused.is_set()
            and pause_src == "guard_transient"
            and (getattr(self, "autopilot_booster_enabled", False) or getattr(self, "autopilot_core_enabled", False))
            and self._guard_consecutive_ok >= self.GUARD_RECOVERY_OK_THRESHOLD
        ):
            self.entry_paused.clear()
            self.entry_pause_reason = ""
            self.entry_paused_at = 0.0
            self._entry_pause_source = ""
            self.add_log(
                f"✅ Smart Guard восстановился ({self._guard_consecutive_ok} успешных циклов подряд): "
                "входы автоматически сняты с паузы."
            )

    def _run_guard_cycle_for_controller(
        self,
        ctrl: BybitV5DirectController,
        transient_errors: Optional[List[str]] = None,
        critical_errors: Optional[List[str]] = None,
    ) -> set:
        ctrl.spm.split_tp_enabled = self.split_tp_enabled
        actions = ctrl.spm.audit_and_protect_open_positions()
        for act in actions:
            if not act.get("ok"):
                self.add_log("ОШИБКА ЗАЩИТЫ: " + str(act))
                act_name = str(act.get("action", "UNKNOWN"))
                msg = "Ошибка защиты позиции: " + act_name
                if act_name in self.CRITICAL_GUARD_ACTIONS:
                    if critical_errors is not None:
                        critical_errors.append(msg)
                    else:
                        self.entry_paused.set()
                        self.entry_pause_reason = msg
                        self.entry_paused_at = time.time()
                        self._entry_pause_source = "guard_critical"
                else:
                    if transient_errors is not None:
                        transient_errors.append(msg)
                    else:
                        self.entry_paused.set()
                        self.entry_pause_reason = msg
                        self.entry_paused_at = time.time()
                        self._entry_pause_source = "guard_transient"
            if act.get("ok"):
                self.add_log("🛡 Smart Guard (фон): " + str(act.get("action")) + " для " + str(act.get("symbol")))

        now_ms = int(time.time() * 1000)
        pure_sltp_keys = ("S1_TREND_RETEST", "S3_VWAP_RANGE", "B1_SQUEEZE_BREAKOUT", "B2_DONCHIAN_INTRADAY", "B3_VWAP_MOMENTUM_IMPULSE", "B4_ORB_SESSION_BREAKOUT", "B5_OI_TREND_EXPANSION", "B6_EMA_PULLBACK_TREND", "B7_FUNDING_SQUEEZE_MOMENTUM", "T1_DONCHIAN_TSMOM", "T2_RS_BTC_BREAKOUT", "T3_KELTNER_TREND_4H", "C2_MTF_COMBO_BREAKOUT", "C3_ENSEMBLE_VOTE_2OF3")
        open_orders = ctrl.get_open_orders()
        for o in open_orders:
            if bool(o.get("reduceOnly")) or o.get("orderType") != "Limit":
                continue
            o_sym = str(o.get("symbol", ""))
            o_side = str(o.get("side", ""))
            o_strat = ctrl.ledger.pending_strategy(o_sym, o_side)
            c_ms = int(o.get("createdTime", 0) or 0)
            ttl_min = (
                45 if any(k in o_strat for k in ("S1_TREND_RETEST", "B1_SQUEEZE_BREAKOUT", "B2_DONCHIAN_INTRADAY", "B3_VWAP_MOMENTUM_IMPULSE", "B4_ORB_SESSION_BREAKOUT", "B5_OI_TREND_EXPANSION", "B6_EMA_PULLBACK_TREND", "B7_FUNDING_SQUEEZE_MOMENTUM")) else (
                    30 if "S3_VWAP_RANGE" in o_strat else (
                        90 if "AUTOTRADE_MOMENTUM_FUNDING" in o_strat else (
                            20 if o_strat.startswith("BOOSTER:") else 0
                        )
                    )
                )
            )
            if ttl_min > 0 and c_ms > 0 and (now_ms - c_ms) >= ttl_min * 60 * 1000:
                c_ord = ctrl.cancel_order(o_sym, str(o.get("orderId", "")))
                if c_ord.get("ok"):
                    self.add_log(f"⏱ [Limit TTL] Отменён неисполненный лимит {o_sym} (>{ttl_min} мин)")

        positions = ctrl.get_positions()
        active_syms = {str(p.get("symbol", "")) for p in positions}
        for p in positions:
            sym = str(p.get("symbol", ""))
            side = str(p.get("side", "Buy"))
            size_str = str(p.get("size", "0"))
            avg_px = float(p.get("avgPrice", 0) or 0)
            mark_px = float(p.get("markPrice", 0) or avg_px)
            cur_sl = float(p.get("stopLoss", 0) or 0)
            p_idx = int(p.get("positionIdx", 0) or 0)
            if avg_px <= 0 or not p.get("entry_intent_id"):
                continue
            intent = ctrl.ledger.active_intent(sym, side, size_str)
            plan = (intent or {}).get("exit_plan", {})
            strat_s = str(p.get("strategy_id") or "")
            hold = plan.get(
                "max_hold_minutes",
                14400 if "T1_DONCHIAN_TSMOM" in strat_s else (
                    10080 if "T3_KELTNER_TREND_4H" in strat_s else (
                        2880 if "T2_RS_BTC_BREAKOUT" in strat_s else (
                            2160 if "C2_MTF_COMBO_BREAKOUT" in strat_s else (
                                1440 if "C3_ENSEMBLE_VOTE_2OF3" in strat_s else (
                                    1080 if any(k in strat_s for k in ("S1_TREND_RETEST", "B1_SQUEEZE_BREAKOUT", "B2_DONCHIAN_INTRADAY", "B7_FUNDING_SQUEEZE_MOMENTUM")) else (
                                        720 if any(k in strat_s for k in ("B5_OI_TREND_EXPANSION", "B6_EMA_PULLBACK_TREND")) else (
                                            480 if any(k in strat_s for k in ("AUTOTRADE_MOMENTUM_FUNDING", "B3_VWAP_MOMENTUM_IMPULSE", "B4_ORB_SESSION_BREAKOUT")) else (
                                                240 if "S3_VWAP_RANGE" in strat_s else (180 if strat_s.startswith("BOOSTER") else 0)
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                ),
            )
            if hold and p.get("opened_ms") and (time.time() * 1000 - p["opened_ms"]) >= hold * 60000:
                pending = [o for o in open_orders if self._is_active_market_close_order(o, sym)]
                if not pending:
                    ctrl.close_position(sym, side, size_str, p_idx)
                    self.add_log("⏱ Лимит времени стратегии: отправлено закрытие " + sym)
                continue
            move_pct = ((mark_px - avg_px) / avg_px) * 100.0 if side == "Buy" else ((avg_px - mark_px) / avg_px) * 100.0
            is_be = (side == "Buy" and cur_sl >= avg_px) or (side == "Sell" and 0 < cur_sl <= avg_px)

            if hasattr(self, "x_radar") and self.x_radar.enabled and hasattr(self.x_radar, "evaluate_open_position"):
                n_eval = self.x_radar.evaluate_open_position(sym, side)
                n_act = n_eval.get("action", "HOLD")
                if n_act == "CLOSE_FULL":
                    c_res = ctrl.close_position(sym, side, size_str, p_idx)
                    if c_res.get("ok"):
                        self.add_log("🚨 [News Guard] Экстренное закрытие " + sym + " (" + side + "): " + str(n_eval.get("reason")))
                    continue
                elif n_act == "PROTECT" and not is_be and move_pct >= 0.18:
                    p_strat = str(p.get("strategy_id") or ctrl.ledger.latest_strategy(sym, side))
                    if not any(x in p_strat for x in pure_sltp_keys):
                        be_res = ctrl.spm.move_sl_to_breakeven(sym, side, avg_px, p_idx)
                        ctrl.spm.activate_trailing_stop(sym, mark_px, ctrl.spm.trailing_step_pct, p_idx)
                        if be_res.get("ok"):
                            self.add_log("🛡 [News Guard] Досрочный БУ + Трейлинг для " + sym + ": " + str(n_eval.get("reason")))
                            is_be = True

            if self.regime_guard.time_stop_enabled and not is_be and not any(x in strat_s for x in pure_sltp_keys + ("BOOSTER:",)):
                age_min = self.get_position_age_minutes(p)
                if age_min >= self.regime_guard.time_stop_minutes and abs(move_pct) <= self.regime_guard.time_stop_band_pct:
                    c_res = ctrl.close_position(sym, side, size_str, p_idx)
                    if c_res.get("ok"):
                        self.add_log(
                            "⏱ Time-Stop сработал для " + sym + ": сделка висела " + str(age_min)
                            + " мин без импульса (" + "{:+.2f}%".format(move_pct) + ") — отправлена заявка на закрытие."
                        )
        # Синхронизация истории вынесена ПОСЛЕ защиты позиций и не блокирует критический цикл
        try:
            ctrl.get_closed_pnl()
        except Exception as h_exc:
            self.add_log("⚠️ [Guard History] Не удалось обновить историю сделок (защита позиций выполнена): " + str(h_exc)[:150])
        return active_syms

    @staticmethod
    def _strategy_priority_rank(strategy_id: str) -> int:
        """Жёсткий приоритет тикеров: Tier 1 (WFA PASSED) -> Tier 2 (Комбо) -> Tier 3 (Экспериментальные)."""
        ranks = {
            "T1_DONCHIAN_TSMOM": 0,
            "B1_SQUEEZE_BREAKOUT": 1,
            "C3_ENSEMBLE_VOTE_2OF3": 2,
            "C2_MTF_COMBO_BREAKOUT": 3,
            "B2_DONCHIAN_INTRADAY": 4,
            "T2_RS_BTC_BREAKOUT": 5,
            "T3_KELTNER_TREND_4H": 6,
            "B3_VWAP_MOMENTUM_IMPULSE": 7,
            "B4_ORB_SESSION_BREAKOUT": 8,
            "B5_OI_TREND_EXPANSION": 9,
            "B6_EMA_PULLBACK_TREND": 10,
            "B7_FUNDING_SQUEEZE_MOMENTUM": 11,
            "S1_TREND_RETEST": 12,
        }
        return ranks.get(str(strategy_id or ""), 20)

    def _is_demo_lab_active(self) -> bool:
        cfg = self.settings.get("demo_lab", {}) if isinstance(self.settings, dict) else {}
        return bool(cfg.get("enabled", False) and self.bybit.base_url == "https://api-demo.bybit.com")

    @staticmethod
    def _demo_lab_booster_sizing_metrics(b_metrics: Dict[str, Any], demo_lab_active: bool) -> Dict[str, Any]:
        """В Demo Lab на общем Demo-счёте Booster-стратегии используют риск 1.5% ($15) от $1,000-пода."""
        m = dict(b_metrics or {})
        if demo_lab_active and not bool(m.get("isolated_subaccount", False)):
            pod_cap = float(m.get("risk_capital_usd", 1000.0) or 1000.0)
            target_risk = round(pod_cap * 0.015, 2)
            if target_risk > float(m.get("effective_risk_usd", 0.0) or 0.0):
                m["effective_risk_usd"] = target_risk
                m["effective_risk_pct"] = 1.5
        return m

    def _live_bbo_map(self) -> Dict[str, Tuple[float, float]]:
        bbo: Dict[str, Tuple[float, float]] = {}
        if hasattr(self.ws_hub, "get_bbo"):
            syms = list(getattr(self.ws_hub, "symbols", None) or self.settings.get("universe", {}).get("core_pairs", []))
            for sym in syms:
                try:
                    bid, ask = self.ws_hub.get_bbo(sym, max_age=30)
                    if bid > 0 and ask > bid:
                        bbo[sym] = (float(bid), float(ask))
                except Exception:
                    continue
            if bbo:
                return bbo
        ws_sum = self.ws_hub.get_summary()
        for m in (ws_sum.get("metrics") or []):
            sym = str(m.get("symbol") or "")
            mid = float(m.get("mid_price") or 0.0)
            sp_bps = float(m.get("spread_bps") or 2.0)
            if sym and mid > 0:
                half = mid * (sp_bps / 20000.0)
                bbo[sym] = (mid - half, mid + half)
        return bbo

    def _run_smart_guard_tick(self) -> None:
        self.guard_status["heartbeat"] = time.time()
        if hasattr(self, "shadow_engine"):
            try:
                self.shadow_engine.update_open_positions(self._live_bbo_map())
            except Exception:
                pass
        if not self.smart_guard_enabled:
            return
        try:
            self._check_dead_mans_switch()
            active_syms: set = set()
            transient_errors: List[str] = []
            critical_errors: List[str] = []
            for ctrl in self._active_controllers():
                try:
                    active_syms |= self._run_guard_cycle_for_controller(
                        ctrl,
                        transient_errors=transient_errors,
                        critical_errors=critical_errors,
                    )
                except Exception as c_exc:
                    err_txt = str(c_exc)[:180]
                    transient_errors.append("Ошибка Smart Guard: " + err_txt)
                    self.add_log("Ошибка Smart Guard (контур): " + err_txt)
            if not transient_errors and not critical_errors:
                lock = getattr(self, "_state_save_lock", None)
                if lock is not None:
                    with lock:
                        for old_s in list(self.position_opened_ts.keys()):
                            if old_s not in active_syms:
                                del self.position_opened_ts[old_s]
                else:
                    for old_s in list(self.position_opened_ts.keys()):
                        if old_s not in active_syms:
                            del self.position_opened_ts[old_s]
            self._handle_guard_cycle_outcome(transient_errors, critical_errors)
        except Exception as exc:
            err_txt = "Ошибка Smart Guard: " + str(exc)[:180]
            self.add_log(err_txt)
            self._handle_guard_cycle_outcome([err_txt], [])
        finally:
            self.guard_status["heartbeat"] = time.time()

    def _smart_guard_loop(self) -> None:
        while True:
            time.sleep(5)
            self._run_smart_guard_tick()

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
            closed_list = list(getattr(self, "_last_booster_closed", None) or self._snapshot.get("closed_pnl") or [])
            pos_list = list(getattr(self, "_last_booster_positions", None) or self._snapshot.get("positions") or [])
            if hasattr(self, "booster"):
                b_metrics = self._demo_lab_booster_sizing_metrics(self.booster.compute_metrics(closed_list, pos_list), self._is_demo_lab_active())
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
        return {"ok":liveness and not reasons,"liveness_ok":liveness,"version":getattr(self,"release_manifest",{}).get("version",DEFAULT_RELEASE_VERSION),
                "build_sha256":getattr(self,"release_manifest",{}).get("build_sha256"),
                "autopilot_alive":alive,"entry_paused":self.entry_paused.is_set(),"entry_pause_reason":getattr(self,"entry_pause_reason",""),"entry_pause_source":getattr(self,"_entry_pause_source",""),"entry_paused_at":getattr(self,"entry_paused_at",0.0),"scan":scan,
                "guard":guard,"snapshot_age_sec":round(age,1) if age is not None else None,
                "snapshot_error":self._snapshot_error,"readiness_errors":reasons}

    def cached_state(self):
        with self._snapshot_lock:result=copy.deepcopy(self._snapshot)
        result['health']=self.health()
        result['manual_task']=dict(self.manual_task)
        ap=result.setdefault('autopilot',{})
        ap.update(booster_enabled=self.autopilot_booster_enabled,core_enabled=self.autopilot_core_enabled,any_enabled=self.autopilot_booster_enabled or self.autopilot_core_enabled,booster_cycles_done=self.booster_cycles_done,core_cycles_done=self.core_cycles_done,next_in_sec=max(0,int(self.autopilot_interval_sec-(time.time()-self._last_auto_ts))))
        result['logs']=self._get_logs_copy()
        return result

    def refresh_funding_rates(self) -> bool:
        try:
            tickers = self.engine.client.get_tickers() or []
            rates = {
                str(t["symbol"]): float(t.get("fundingRate", 0) or 0)
                for t in tickers
                if t.get("symbol") and t.get("fundingRate") not in (None, "")
            }
            if rates:
                self.funding_rates = rates
                self.funding_updated_at = time.time()
                return True
            return False
        except Exception as exc:
            self.add_log("⚠️ Не удалось обновить ставки фандинга: " + str(exc)[:150])
            return False

    def _get_all_positions(self) -> List[Dict[str, Any]]:
        combined: List[Dict[str, Any]] = []
        for ctrl in self._active_controllers():
            combined.extend(ctrl.get_positions())
        return combined

    def _get_all_closed_pnl(self, reconcile: bool = True) -> List[Dict[str, Any]]:
        combined: List[Dict[str, Any]] = []
        for ctrl in self._active_controllers():
            try:
                rows = ctrl.get_closed_pnl(reconcile=reconcile)
            except TypeError:
                rows = ctrl.get_closed_pnl()
            combined.extend(rows or [])
        return combined

    def _prefetch_oi_squeeze(self) -> List[Dict[str, Any]]:
        demo_lab_cfg_pre = self.settings.get("demo_lab", {}) if isinstance(self.settings, dict) else {}
        demo_lab_pre = bool(demo_lab_cfg_pre.get("enabled", False) and self.bybit.base_url == "https://api-demo.bybit.com")
        now_oi = time.time()
        if demo_lab_pre and getattr(self, "_oi_scan_cache", None) and (now_oi - float(getattr(self, "_oi_scan_ts", 0.0))) < 1800.0:
            return list(self._oi_scan_cache)
        core_pairs_pre = self.settings.get("universe", {}).get("core_pairs", [])
        ws_sum_pre = self.ws_hub.get_summary()
        try:
            squeeze_list = self.oi_scanner.scan_squeeze_setups(core_pairs_pre, ws_sum_pre)
        except Exception:
            squeeze_list = []
        for s in squeeze_list:
            s["sector"] = self.regime_guard.get_sector(s.get("symbol", ""))
        self._oi_scan_cache = list(squeeze_list)
        self._oi_scan_ts = now_oi
        return squeeze_list

    def run_scan(self, trade_booster=False, trade_core=False):
        # Сетевой опрос OISqueezeScanner выполняется ВНЕ глобального self._lock
        squeeze_list = self._prefetch_oi_squeeze()
        with self._lock:
            self.scan_status.update(running=True,last_attempt=time.time(),last_error="")
            try:
                r=self._run_scan(trade_booster,trade_core,squeeze_list=squeeze_list)
                self.scan_status['last_success']=time.time()
                self.scan_status['failed_cycles']=0
                return r
            except Exception as exc:
                self.scan_status['failed_cycles']+=1
                self.scan_status['last_error']=str(exc)[:250]
                raise
            finally:self.scan_status['running']=False

    def _run_scan(self, trade_booster: bool = False, trade_core: bool = False, squeeze_list: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        if squeeze_list is None:
            squeeze_list = self._prefetch_oi_squeeze()

        with self._lock:
            self.refresh_funding_rates()
            core_pairs = self.settings.get("universe", {}).get("core_pairs", [])
            ws_sum = self.ws_hub.get_summary()
            btc_reg = self.regime_guard.update_btc_regime(ws_sum)
            news_sum = self.x_radar.get_summary() if hasattr(self.x_radar, "get_summary") else {}

            scan_res = self.engine.scan_market(auto_execute_demo=False)
            if hasattr(self, "strategy_funnel"):
                self.strategy_funnel.record_scan(scan_res.get("strategy_diagnostics") or {})
            if hasattr(self, "shadow_engine"):
                try:
                    bbo_now = self._live_bbo_map()
                    self.shadow_engine.update_open_positions(bbo_now)
                    self.shadow_engine.record_signals(scan_res.get("shadow_candidates") or scan_res.get("approved_signals") or [], bbo_now)
                except Exception:
                    pass
            scan_res["oi_squeeze_signals"] = squeeze_list
            scan_res["btc_regime"] = btc_reg

            positions = self._get_all_positions()
            closed_pnl = self.bybit.get_closed_pnl()
            b_ctrl_m = getattr(self, "booster_bybit", self.bybit)
            b_pos_m = b_ctrl_m.get_positions() if b_ctrl_m is not self.bybit else positions
            b_closed_m = b_ctrl_m.get_closed_pnl() if b_ctrl_m is not self.bybit else closed_pnl
            all_closed_pnl = (list(closed_pnl) + list(b_closed_m)) if b_ctrl_m is not self.bybit else closed_pnl
            cal_summary = self.ml_calibrator.calibrate_from_closed_pnl(all_closed_pnl)
            if self.ml_calibrator.state.get("enabled", True):
                self.core_min_prob_pct = float(cal_summary.get("recommended_ml_threshold_pct", self.core_min_prob_pct))
            b_metrics = self.booster.compute_metrics(b_closed_m, b_pos_m)

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

            demo_lab_cfg_run = self.settings.get("demo_lab", {}) if isinstance(self.settings, dict) else {}
            demo_lab_active = bool(demo_lab_cfg_run.get("enabled", False) and self.bybit.base_url == "https://api-demo.bybit.com")
            booster_strat_ids = {"B1_SQUEEZE_BREAKOUT", "B2_DONCHIAN_INTRADAY", "B3_VWAP_MOMENTUM_IMPULSE", "B4_ORB_SESSION_BREAKOUT", "B5_OI_TREND_EXPANSION", "B6_EMA_PULLBACK_TREND", "B7_FUNDING_SQUEEZE_MOMENTUM", "S1_TREND_RETEST"}
            exec_queue: List[Tuple[str, Dict[str, Any]]] = []
            booster_slots = 0
            core_slots = 0

            if trade_booster and self.booster.state.get("enabled", True) and not self.entry_paused.is_set():
                if b_metrics["circuit_breaker_tripped"] and not (demo_lab_active and demo_lab_cfg_run.get("disable_daily_stops_on_demo", False)):
                    self.add_log("🛑 [Разгон $1k→$10k] Сработал дневной стоп-кран или стоп цикла. Входы разгона приостановлены.")
                else:
                    booster_pos = [p for p in positions if str(p.get("strategy_id", "")).startswith("BOOSTER:")]
                    max_b_slots = max(int(b_metrics["max_positions"]), 12) if demo_lab_active else int(b_metrics["max_positions"])
                    booster_slots = max(0, max_b_slots - len(booster_pos))
                    if booster_slots > 0:
                        for s in (scan_res.get("approved_signals") or []):
                            if (
                                s.get("strategy_id") in booster_strat_ids
                                and s.get("passed_ml_filter")
                                and s.get("debate_verdict") == "APPROVED"
                                and s.get("cal_status") != "QUARANTINE"
                            ):
                                exec_queue.append(("BOOSTER", s))

            if trade_core and not self.entry_paused.is_set():
                eff_core_max = max(self.core_max_positions, 8) if demo_lab_active else self.core_max_positions
                core_slots = max(0, eff_core_max - len([p for p in positions if str(p.get("strategy_id", "")).startswith("CORE:")]))
                min_prob = self.core_min_prob_pct / 100.0
                if core_slots > 0:
                    for s in (scan_res.get("approved_signals", []) or []):
                        if (
                            s.get("strategy_id") not in booster_strat_ids
                            and s.get("passed_ml_filter")
                            and s.get("debate_verdict") == "APPROVED"
                            and s.get("cal_status") != "QUARANTINE"
                            and (
                                ((s.get("features") or {}).get("ml_calibration_status") in ("WARMING_UP", "DEMO_LAB_EXPERIMENTAL"))
                                or float(s.get("ml_win_probability", 0) or 0) >= (0.60 if s.get("strategy_id") in ("S3_VWAP_RANGE", "AUTOTRADE_MOMENTUM_FUNDING") else min_prob)
                            )
                        ):
                            exec_queue.append(("CORE", s))

            # Единая приоритетная очередь: Tier 1 (T1, B1) -> Tier 2 (C3, C2) -> Tier 3 (экспериментальные)
            exec_queue.sort(key=lambda item: (self._strategy_priority_rank(str(item[1].get("strategy_id", ""))), -float(item[1].get("ml_win_probability", 0) or 0)))
            b_sizing_metrics = self._demo_lab_booster_sizing_metrics(b_metrics, demo_lab_active)
            if exec_queue and (time.time() - float(getattr(self, "funding_updated_at", 0.0) or 0.0) > 600.0):
                self.refresh_funding_rates()

            for kind, sig in exec_queue:
                if kind == "BOOSTER":
                    if booster_slots <= 0:
                        continue
                    side = sig["side"]
                    strat_code = str(sig.get("strategy_id", "S1_TREND_RETEST"))
                    if not (demo_lab_active and demo_lab_cfg_run.get("disable_daily_stops_on_demo", False)):
                        on_cd, cd_left = self.booster.is_symbol_on_cooldown(sig["symbol"])
                        if on_cd:
                            if hasattr(self, "strategy_funnel"):
                                self.strategy_funnel.record_guard_block(strat_code, f"Кулдаун {sig['symbol']} ({cd_left} мин)")
                            self.add_log("⏳ [Кулдаун после стопа] Пропуск " + sig["symbol"] + ": еще " + str(cd_left) + " мин")
                            continue
                    try:
                        allowed, guard_msg = self.regime_guard.can_open_position(sig["symbol"], side, positions, ws_sum, strategy_id="BOOSTER:" + strat_code)
                    except TypeError:
                        allowed, guard_msg = self.regime_guard.can_open_position(sig["symbol"], side, positions, ws_sum)
                    n_mod = self.x_radar.get_coin_modifier(sig["symbol"], "LONG" if side == "Buy" else "SHORT")
                    if not allowed or not n_mod["allowed"]:
                        if hasattr(self, "strategy_funnel"):
                            self.strategy_funnel.record_guard_block(strat_code, guard_msg if not allowed else str(n_mod.get("reason", "X/News")))
                        continue
                    sizing = self.booster.calculate_order_qty(float(sig["entry_price"]), float(sig["stop_loss"]), b_sizing_metrics)
                    w_mult = float(sig.get("cal_weight", 1.0) or 1.0)
                    adj_qty = round(sizing["qty_coins"] * w_mult, 6)
                    b_otype = sig.get("order_type") if sig.get("order_type") in ("Market", "Limit") else "Limit"
                    b_ctrl = getattr(self, "booster_bybit", self.bybit)
                    r = b_ctrl.place_order(
                        sig["symbol"], side, b_otype, adj_qty,
                        float(sig["entry_price"]), float(sig.get("take_profit") or sig["take_profit_2"]), float(sig["stop_loss"]),
                        sizing["leverage"], False, strategy_id="BOOSTER:" + strat_code,
                        signal_context={"features": sig.get("features", {}), "ml_score": sig.get("ml_win_probability"), "cal_weight": w_mult, "version": getattr(self, "release_manifest", {}).get("version", DEFAULT_RELEASE_VERSION)}
                    )
                    if not r.get("ok") and hasattr(self, "strategy_funnel"):
                        self.strategy_funnel.record_guard_block(strat_code, str(r.get("retMsg", "Ошибка биржи")))
                    if r.get("ok"):
                        if hasattr(self, "strategy_funnel"):
                            self.strategy_funnel.record_submit(strat_code)
                        booster_slots -= 1
                        positions = self._get_all_positions()
                        self.mark_position_source(sig["symbol"], "🚀 Booster A+")
                        self.booster.register_booster_symbol(sig["symbol"])
                        self.ml_calibrator.register_entry(sig["symbol"], strat_code)
                        self.add_log("🚀 [Разгон " + strat_code + "] Вход (" + b_otype + ") " + sig["symbol"] + " " + side + " qty=" + str(r.get("qty")))
                else:
                    if core_slots <= 0:
                        continue
                    side = sig["side"]
                    if side not in ("Buy", "Sell"): raise ValueError("Invalid signal side")
                    core_strat_id = str(sig.get("strategy_id", "UNKNOWN"))
                    try:
                        allowed, guard_msg = self.regime_guard.can_open_position(sig["symbol"], side, positions, ws_sum, strategy_id="CORE:" + core_strat_id)
                    except TypeError:
                        allowed, guard_msg = self.regime_guard.can_open_position(sig["symbol"], side, positions, ws_sum)
                    n_mod = self.x_radar.get_coin_modifier(sig["symbol"], sig.get("direction", sig.get("side", "LONG")))
                    if not allowed or not n_mod["allowed"]:
                        why = guard_msg if not allowed else n_mod["reason"]
                        if hasattr(self, "strategy_funnel"):
                            self.strategy_funnel.record_guard_block(core_strat_id, why)
                        self.add_log("🛡 [Основная ИИ Фильтр] Пропуск " + str(sig["symbol"]) + " (" + side + "): " + why)
                        continue
                    sig_otype = sig.get("order_type") if sig.get("order_type") in ("Market", "Limit") else order_type
                    sig_split_tp = False if sig.get("strategy_id") in ("S1_TREND_RETEST", "S3_VWAP_RANGE", "AUTOTRADE_MOMENTUM_FUNDING", "B1_SQUEEZE_BREAKOUT", "B2_DONCHIAN_INTRADAY", "B3_VWAP_MOMENTUM_IMPULSE", "B4_ORB_SESSION_BREAKOUT", "B5_OI_TREND_EXPANSION", "B6_EMA_PULLBACK_TREND", "B7_FUNDING_SQUEEZE_MOMENTUM", "T1_DONCHIAN_TSMOM", "T2_RS_BTC_BREAKOUT", "T3_KELTNER_TREND_4H", "C2_MTF_COMBO_BREAKOUT", "C3_ENSEMBLE_VOTE_2OF3") else self.split_tp_enabled
                    r = self.bybit.place_order(
                        sig["symbol"], side, sig_otype, float(sig.get("position_size_coins", 0) or 0),
                        float(sig.get("entry_price", 0) or 0), float(sig.get("take_profit", 0) or 0),
                        float(sig.get("stop_loss", 0) or 0), self.max_leverage, sig_split_tp, strategy_id="CORE:" + core_strat_id, signal_context={"features": sig.get("features", {}), "ml_score": sig.get("ml_win_probability"), "cal_weight": sig.get("cal_weight", 1.0), "version": getattr(self, "release_manifest", {}).get("version", DEFAULT_RELEASE_VERSION)}
                    )
                    if not r.get("ok"):
                        if hasattr(self, "strategy_funnel"):
                            self.strategy_funnel.record_guard_block(core_strat_id, str(r.get("retMsg", "Ошибка биржи")))
                        self.add_log("Вход Core отклонён: " + str(r.get("retMsg")))
                    if r.get("ok"):
                        if hasattr(self, "strategy_funnel"):
                            self.strategy_funnel.record_submit(core_strat_id)
                        core_slots -= 1
                        positions = self._get_all_positions()
                        self.mark_position_source(sig["symbol"], "🎯 ML Core")
                        self.ml_calibrator.register_entry(sig["symbol"], str(sig.get("strategy_id") or sig.get("strategy_name", "S1_TREND_RETEST")))
                        self.add_log(
                            "🎯 [Автопилот Основной ИИ] Открыт " + str(sig["symbol"]) + " " + side
                            + " [" + guard_msg + ", ML " + str(round(float(sig.get("ml_win_probability", 0)) * 100, 1)) + "%]"
                        )

            self.last_scan = scan_res
            self.last_scan_ts = datetime.now().strftime("%H:%M:%S")
            return scan_res

    @staticmethod
    def _load_wfa_validation_summary() -> Dict[str, Any]:
        p = BASE_DIR / "data" / "wfa_validation.json"
        if not p.exists():
            return {"updated_at": "—", "strategies": {}}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"updated_at": "—", "strategies": {}}

    def get_full_state(self) -> Dict[str, Any]:
        acct = self.bybit.get_account_summary()
        try:
            closed_pnl = self.bybit.get_closed_pnl(reconcile=False)
        except TypeError:
            closed_pnl = self.bybit.get_closed_pnl()
        positions = [dict(p, account="autotrade") for p in self.bybit.get_positions()]
        open_orders = [dict(o, account="autotrade") for o in self.bybit.get_open_orders()]
        b_ctrl = getattr(self, "booster_bybit", self.bybit)
        b_acct = None
        if b_ctrl is not self.bybit:
            try:
                b_closed = b_ctrl.get_closed_pnl(reconcile=False)
            except TypeError:
                b_closed = b_ctrl.get_closed_pnl()
        else:
            b_closed = closed_pnl
        b_positions = [dict(p, account="booster") for p in b_ctrl.get_positions()] if b_ctrl is not self.bybit else positions
        if b_ctrl is not self.bybit:
            try:
                b_acct = b_ctrl.get_account_summary()
            except Exception:
                b_acct = None
            positions.extend(b_positions)
            open_orders.extend([dict(o, account="booster") for o in b_ctrl.get_open_orders()])
        ws_sum = self.ws_hub.get_summary()
        btc_reg = self.regime_guard.update_btc_regime(ws_sum)

        guard_maps = {"autotrade": {}}
        ctrl_urls = {"autotrade": str(getattr(self.bybit, "base_url", "")).rstrip("/")}
        try:
            guard_maps["autotrade"] = self.bybit.spm.load_guard() if hasattr(self.bybit.spm, "load_guard") else {}
        except Exception:
            pass
        if b_ctrl is not self.bybit:
            ctrl_urls["booster"] = str(getattr(b_ctrl, "base_url", "")).rstrip("/")
            try:
                guard_maps["booster"] = b_ctrl.spm.load_guard() if hasattr(b_ctrl.spm, "load_guard") else {}
            except Exception:
                guard_maps["booster"] = {}

        entry_orders_cnt = 0
        protective_orders_cnt = 0
        for o in open_orders:
            stop_t = str(o.get("stopOrderType") or "").strip()
            is_prot = bool(o.get("reduceOnly")) or (bool(stop_t) and stop_t.lower() != "unknown")
            o["order_role"] = "protective" if is_prot else "entry"
            if is_prot:
                protective_orders_cnt += 1
            else:
                entry_orders_cnt += 1

        for p in positions:
            sym = p.get("symbol", "")
            acc_id = p.get("account", "autotrade")
            p_idx = int(p.get("positionIdx", 0) or 0)
            side = p.get("side", "Buy")
            avg_px = float(p.get("avgPrice", 0) or 0)
            cur_sl = float(p.get("stopLoss", 0) or 0)
            cur_trail = float(p.get("trailingStop", 0) or 0)
            g_map = guard_maps.get(acc_id) or {}
            acc_url = ctrl_urls.get(acc_id, ctrl_urls["autotrade"])
            intent_id_p = str(p.get("entry_intent_id") or "").strip()
            g_rec = g_map.get(f"{acc_url}:{sym}:{p_idx}:{intent_id_p}") if intent_id_p else None
            if not g_rec:
                prefix = f"{acc_url}:{sym}:{p_idx}:"
                g_rec = next((v for k, v in g_map.items() if str(k).startswith(prefix)), None) or {}
            expected_tp1_id = str(g_rec.get("tp1_order") or "").strip()
            tp1_ord = None
            if expected_tp1_id:
                tp1_ord = next((
                    o for o in open_orders
                    if o.get("account", "autotrade") == acc_id and str(o.get("orderId") or "") == expected_tp1_id
                ), None)
            if tp1_ord is None:
                tp1_ord = next((
                    o for o in open_orders
                    if o.get("account", "autotrade") == acc_id
                    and o.get("symbol") == sym
                    and int(o.get("positionIdx", 0) or 0) == p_idx
                    and bool(o.get("reduceOnly"))
                    and o.get("orderType") == "Limit"
                    and not str(o.get("stopOrderType") or "").strip()
                ), None)
            p["tp1_order_id"] = tp1_ord.get("orderId", "") if tp1_ord else expected_tp1_id
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

        # Агрегируем закрытия новых стратегий по entry_intent_id (частичные TP1 + финал = 1 позиция)
        new_pos_pnl: Dict[str, float] = {}
        legacy_pnl = 0.0
        legacy_cnt = 0
        for c in closed_pnl:
            sid_c = str(c.get("strategy_id") or "UNATTRIBUTED")
            pnl_c = float(c.get("closedPnl", 0) or 0)
            if sid_c in ("", "UNATTRIBUTED", "MANUAL", "LEGACY"):
                legacy_pnl += pnl_c
                legacy_cnt += 1
            else:
                key_c = str(c.get("entry_intent_id") or c.get("orderId") or f"{c.get('symbol')}:{c.get('updatedTime')}")
                new_pos_pnl[key_c] = new_pos_pnl.get(key_c, 0.0) + pnl_c
        new_strat_pnl = round(sum(new_pos_pnl.values()), 2)
        new_strat_wins = sum(1 for v in new_pos_pnl.values() if v > 0)
        new_strat_wr = round((new_strat_wins / len(new_pos_pnl)) * 100.0, 1) if new_pos_pnl else 0.0

        any_active = self.autopilot_booster_enabled or self.autopilot_core_enabled
        next_in_sec = 0
        if any_active and self._last_auto_ts > 0:
            next_in_sec = max(0, int(self.autopilot_interval_sec - (time.time() - self._last_auto_ts)))

        demo_lab_on = self._is_demo_lab_active()
        demo_lab_cfg_st = self.settings.get("demo_lab", {}) if isinstance(self.settings, dict) else {}
        self._last_booster_closed = list(b_closed)
        self._last_booster_positions = list(b_positions)
        booster_metrics = self._demo_lab_booster_sizing_metrics(self.booster.compute_metrics(b_closed, b_positions), demo_lab_on)
        now_m = time.time()
        if not getattr(self, "_live_metrics_cache", None) or (now_m - float(getattr(self, "_live_metrics_ts", 0.0))) >= 20.0:
            active_ledgers = [("autotrade", self.bybit.ledger)]
            if b_ctrl is not self.bybit:
                active_ledgers.append(("booster", b_ctrl.ledger))
            self._live_metrics_cache = compute_live_strategy_metrics(active_ledgers)
            self._live_metrics_ts = now_m
        live_strategy_metrics = self._live_metrics_cache
        funnel_summary = (
            self.strategy_funnel.get_summary(live_strategy_metrics.get("filled_24h_by_strategy"))
            if hasattr(self, "strategy_funnel")
            else {"window_hours": 24, "updated_at": "—", "strategies": {}}
        )
        shadow_summary = (
            self.shadow_engine.get_summary(self._live_bbo_map())
            if hasattr(self, "shadow_engine")
            else {"enabled": False, "leaderboard": [], "total_open": 0, "total_closed": 0}
        )

        usdt_bal = float(acct.get("usdt_balance", 0) or 0)
        shared_acc = b_ctrl is self.bybit
        port_cap_usd = min(float(demo_lab_cfg_st.get("portfolio_cap_usdt", 50000.0) or 50000.0), usdt_bal) if demo_lab_on else min(1000.0, usdt_bal)
        port_risk_cap_pct = 25 if demo_lab_on else 6
        port_risk_used = compute_portfolio_risk_usd(positions, open_orders, strict=False)
        core_open_cnt = sum(1 for p in positions if str(p.get("strategy_id", "")).startswith("CORE:"))
        booster_open_cnt = sum(1 for p in positions if str(p.get("strategy_id", "")).startswith("BOOSTER:"))
        core_eff_max = max(self.core_max_positions, 8) if demo_lab_on else self.core_max_positions
        booster_cfg_max = int(booster_metrics.get("max_positions", 2) or 2)
        booster_eff_max = max(booster_cfg_max, 12) if demo_lab_on else booster_cfg_max
        max_conc_cfg = int(demo_lab_cfg_st.get("max_concurrent_positions", 20) or 20) if demo_lab_on else (core_eff_max + booster_eff_max)
        effective_limits = {
            "demo_lab_enabled": demo_lab_on,
            "shared_demo_account": shared_acc,
            "core_configured_max": self.core_max_positions,
            "core_effective_max": core_eff_max,
            "core_open": core_open_cnt,
            "booster_configured_max": booster_cfg_max,
            "booster_effective_max": booster_eff_max,
            "booster_open": booster_open_cnt,
            "portfolio_max_concurrent": max_conc_cfg,
            "portfolio_slots_left": max(0, max_conc_cfg - len(positions)),
            "portfolio_cap_usd": round(port_cap_usd, 2),
            "portfolio_risk_used_usd": round(port_risk_used, 2),
            "portfolio_risk_cap_pct": port_risk_cap_pct,
            "portfolio_risk_cap_usd": round(port_cap_usd * (port_risk_cap_pct / 100.0), 2),
            "capital_note": "Расчётный капитал $1,000/страт. (общий Demo-счёт)" if (demo_lab_on and shared_acc) else "Изолированный расчёт капитала",
        }
        order_counts = {
            "positions": len(positions),
            "entry": entry_orders_cnt,
            "protective": protective_orders_cnt,
            "total_orders": len(open_orders),
        }
        pnl_summary = {
            "total_realized_usd": round(realized_sum, 2),
            "total_records_count": len(closed_pnl),
            "displayed_records_count": min(100, len(closed_pnl)),
            "new_strategies_pnl_usd": new_strat_pnl,
            "new_strategies_positions": len(new_pos_pnl),
            "new_strategies_win_rate_pct": new_strat_wr,
            "legacy_pnl_usd": round(legacy_pnl, 2),
            "legacy_records_count": legacy_cnt,
        }

        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_scan_ts": self.last_scan_ts,
            "account": acct,
            "booster_account": b_acct,
            "env_status": self.env_manager.get_public_status(),
            "realized_pnl_recent": round(realized_sum, 2),
            "closed_trades_count": len(closed_pnl),
            "live_win_rate_pct": win_rate,
            "pnl_summary": pnl_summary,
            "order_counts": order_counts,
            "effective_limits": effective_limits,
            "positions": positions,
            "open_orders": open_orders,
            "closed_pnl": closed_pnl[:100],
            "ws": ws_sum,
            "btc_regime": btc_reg,
            "x_news": self.x_radar.get_summary() if hasattr(self.x_radar, "get_summary") else {},
            "ml_calibration": self.ml_calibrator.get_summary(),
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
            "wfa_validation": self._load_wfa_validation_summary(),
            "strategy_funnel_24h": funnel_summary,
            "live_strategy_metrics": live_strategy_metrics,
            "shadow_lab": shadow_summary,
            "settings": {
                "order_type": self.settings.get("execution", {}).get("order_type", "Market"),
                "max_leverage": self.max_leverage,
                "core_max_positions": self.core_max_positions,
                "core_min_prob_pct": self.core_min_prob_pct,
                "sound_alerts_enabled": self.sound_alerts_enabled,
                "core_pairs": self.settings.get("universe", {}).get("core_pairs", []),
                "demo_lab_enabled": self._is_demo_lab_active(),
                "demo_lab_strategies": list((self.settings.get("demo_lab") or {}).get("admit_experimental_strategies") or []),
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
            "logs": self._get_logs_copy(),
        }

    def get_strategy_detail(self, strategy_id: str) -> Dict[str, Any]:
        """Полный профиль стратегии для детальной страницы: паспорт, WFA, воронка 24ч, Real Demo и Live Shadow."""
        raw_id = str(strategy_id or "").strip()
        sid = raw_id.split(":", 1)[1].strip() if ":" in raw_id else raw_id
        catalog = {
            "T1_DONCHIAN_TSMOM": {"contour": "Автотрейдинг (Core)", "tier": "Tier 1 · WFA PASSED", "timeframe": "4h", "max_hold": "до 10 дней", "order_type": "Market / Chaser", "desc": "Многодневный трендовый пробой Donchian 20/40/80 с vol-scaling таргетингом волатильности и Pure SL/TP (8R)."},
            "B1_SQUEEZE_BREAKOUT": {"contour": "Разгон (Booster)", "tier": "Tier 1 · WFA PASSED", "timeframe": "15m / 1h", "max_hold": "до 18 часов", "order_type": "Limit PostOnly", "desc": "Пробой волатильного сжатия Bollinger/Keltner + Supertrend с фильтром выходных NoWeekendChop и Pure SL/TP."},
            "C3_ENSEMBLE_VOTE_2OF3": {"contour": "Автотрейдинг (Core)", "tier": "Tier 2 · Комбо", "timeframe": "15m–4h", "max_hold": "до 24 часов", "order_type": "Market / Chaser", "desc": "Ансамблевое голосование: вход только при совпадении направления сразу ≥2 независимых стратегий на одной монете (Pure SL/TP 3.5R)."},
            "C2_MTF_COMBO_BREAKOUT": {"contour": "Автотрейдинг (Core)", "tier": "Tier 2 · Комбо", "timeframe": "15m / 1h / 4h", "max_hold": "до 36 часов", "order_type": "Market / Chaser", "desc": "Мультитаймфреймовый комбо-пробой: 4h тренд + относительная сила к BTC + позиция относительно VWAP + 8h пробой."},
            "B2_DONCHIAN_INTRADAY": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "1h / 4h", "max_hold": "до 18 часов", "order_type": "Market / Chaser", "desc": "Внутридневной пробой 12h/24h канала Donchian по направлению 4h-тренда на всех 20 парах."},
            "B3_VWAP_MOMENTUM_IMPULSE": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "15m / 1h", "max_hold": "до 8 часов", "order_type": "Market / Chaser", "desc": "Импульсный отрыв от сессионного VWAP (Z=0.45..2.2σ) с подтверждением RVOL ≥ 1.35."},
            "B4_ORB_SESSION_BREAKOUT": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "15m", "max_hold": "до 8 часов", "order_type": "Market / Chaser", "desc": "Пробой диапазона открытия торговых сессий (Азия 00:00, Европа 07:00, США 13:00 UTC)."},
            "B5_OI_TREND_EXPANSION": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "15m", "max_hold": "до 12 часов", "order_type": "Market / Chaser", "desc": "Пробой 20-барного диапазона с одновременным притоком открытого интереса (ΔOI ≥ +1.2%)."},
            "B6_EMA_PULLBACK_TREND": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "15m / 1h / 4h", "max_hold": "до 12 часов", "order_type": "Market / Chaser", "desc": "Откат к EMA21 в сильном 4h-тренде (1h ADX ≥ 20) с бычьим/медвежьим отскоком по RSI."},
            "B7_FUNDING_SQUEEZE_MOMENTUM": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "1h", "max_hold": "до 18 часов", "order_type": "Market / Chaser", "desc": "Контр-фандинговый сквиз: вход по пробою 4h при перекосе фандинга против толпы."},
            "T2_RS_BTC_BREAKOUT": {"contour": "Автотрейдинг (Core)", "tier": "Tier 3 · Demo Lab", "timeframe": "1h", "max_hold": "до 48 часов", "order_type": "Market / Chaser", "desc": "Пробой альткоинов-лидеров/аутсайдеров с относительной силой ≥ 2.2% к BTC."},
            "T3_KELTNER_TREND_4H": {"contour": "Автотрейдинг (Core)", "tier": "Tier 3 · Demo Lab", "timeframe": "4h", "max_hold": "до 7 дней", "order_type": "Market / Chaser", "desc": "Среднесрочный пробой 4h канала Кельтнера (EMA20 ± 2.2·ATR) с удержанием до 7 дней."},
            "S1_TREND_RETEST": {"contour": "Разгон (Booster)", "tier": "Tier 3 · Demo Lab", "timeframe": "15m / 1h", "max_hold": "до 18 часов", "order_type": "Limit PostOnly", "desc": "Лимитный ретест пробитого уровня по тренду (PostOnly 0.02% Maker)."},
            "S3_VWAP_RANGE": {"contour": "Автотрейдинг (Архив)", "tier": "Заблокирована WFA", "timeframe": "15m", "max_hold": "до 4 часов", "order_type": "Limit PostOnly", "desc": "Возврат к сессионному VWAP во флэте (отключена WFA Gatekeeper)."},
            "AUTOTRADE_MOMENTUM_FUNDING": {"contour": "Автотрейдинг (Архив)", "tier": "Заблокирована WFA", "timeframe": "1h", "max_hold": "до 8 часов", "order_type": "Limit PostOnly", "desc": "Кросс-секционный моментум + фандинг (отключена WFA Gatekeeper)."},
        }
        snap = self.cached_state()
        wfa_map = (snap.get("wfa_validation") or {}).get("strategies") or {}
        funnel_map = (snap.get("strategy_funnel_24h") or {}).get("strategies") or {}
        live_map = (snap.get("live_strategy_metrics") or {}).get("strategies") or {}
        all_traces = (snap.get("live_strategy_metrics") or {}).get("execution_traces") or []
        demo_traces = [t for t in all_traces if str(t.get("strategy_id") or "") == sid]
        demo_closed_chron = [t for t in reversed(demo_traces) if t.get("closed_pnl_usd") is not None]
        demo_curve = [1000.0]
        for t in demo_closed_chron:
            demo_curve.append(round(demo_curve[-1] + float(t.get("closed_pnl_usd") or 0.0), 2))
        if len(demo_curve) == 1:
            demo_curve.append(1000.0)
        demo_open_pos = [
            p for p in (snap.get("positions") or [])
            if (str(p.get("strategy_id") or "").split(":", 1)[-1].strip() == sid)
        ]
        ml_list = (snap.get("ml_calibration") or {}).get("strategy_list") or []
        ml_entry = next((m for m in ml_list if str(m.get("strategy_name") or "").lower() == sid.lower()), None)
        shadow_detail = (
            self.shadow_engine.get_strategy_detail(sid, self._live_bbo_map())
            if hasattr(self, "shadow_engine")
            else {}
        )
        demo_lab_on = self._is_demo_lab_active()
        demo_lab_strats = list((self.settings.get("demo_lab") or {}).get("admit_experimental_strategies") or [])
        wfa_entry = wfa_map.get(sid) or {}
        wfa_passed = bool(wfa_entry.get("wfe_passed", False))
        admitted_demo_lab = bool(demo_lab_on and sid in demo_lab_strats)
        meta = dict(catalog.get(sid, {"contour": "Стратегия", "tier": "—", "timeframe": "—", "max_hold": "—", "order_type": "—", "desc": "Параметры стратегии загружены из истории исполнения."}))
        meta["strategy_id"] = sid
        meta["risk_per_trade"] = "$15 (1.5% от $1,000-пода)" if demo_lab_on else "Консервативный лимит"
        meta["admission_status"] = "WFA_PASSED" if wfa_passed else ("DEMO_LAB" if admitted_demo_lab else "BLOCKED")
        return {
            "ok": True,
            "strategy_id": sid,
            "updated_at": snap.get("timestamp", "—"),
            "meta": meta,
            "wfa": wfa_entry,
            "funnel_24h": funnel_map.get(sid) or {},
            "demo_metrics": live_map.get(sid) or {},
            "demo_equity_curve": demo_curve,
            "demo_open_positions": demo_open_pos,
            "demo_traces": demo_traces[:50],
            "shadow": shadow_detail,
            "ml_cal": ml_entry or {},
        }


class KairosSessionAuth:
    """Опциональная сессионная авторизация для веб-интерфейса KAIROS (включается через KAIROS_AUTH_USER и KAIROS_AUTH_PASSWORD)."""
    COOKIE_NAME = "kairos_session"

    def __init__(self) -> None:
        self._fallback_secret = secrets.token_hex(32)
        self._fail_lock = threading.Lock()
        self._failed_attempts: Dict[str, List[float]] = {}

    @property
    def username(self) -> str:
        return str(os.getenv("KAIROS_AUTH_USER", "") or "").strip()

    @property
    def password(self) -> str:
        return str(os.getenv("KAIROS_AUTH_PASSWORD", "") or "")

    @property
    def password_hash(self) -> str:
        return str(os.getenv("KAIROS_AUTH_PASSWORD_HASH", "") or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.username and (self.password or self.password_hash))

    @property
    def secret(self) -> bytes:
        s = str(os.getenv("KAIROS_AUTH_SECRET", "") or "").strip()
        return (s or self._fallback_secret).encode("utf-8")

    @staticmethod
    def _safe_eq(a: str, b: str) -> bool:
        return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))

    def verify_credentials(self, user: str, pw: str) -> bool:
        if not self.enabled:
            return True
        if not self._safe_eq(str(user or "").strip(), self.username):
            return False
        if self.password_hash:
            expected = self.password_hash.lower()
            if expected.startswith("sha256:"):
                expected = expected.split(":", 1)[1]
            actual = hashlib.sha256(str(pw or "").encode("utf-8")).hexdigest()
            return self._safe_eq(actual, expected)
        return self._safe_eq(str(pw or ""), self.password)

    def _client_ip(self, environ: Dict[str, Any]) -> str:
        real_ip = str(environ.get("HTTP_X_REAL_IP", "") or "").strip()
        if real_ip:
            return real_ip
        fwd_raw = str(environ.get("HTTP_X_FORWARDED_FOR", "") or "").strip()
        if fwd_raw:
            # Берём последний IP, дописанный доверенным reverse-proxy, чтобы исключить спуфинг первого элемента
            return fwd_raw.split(",")[-1].strip()
        return str(environ.get("REMOTE_ADDR", "127.0.0.1") or "127.0.0.1")

    def is_rate_limited(self, environ: Dict[str, Any]) -> bool:
        ip = self._client_ip(environ)
        now = time.time()
        with self._fail_lock:
            attempts = [t for t in self._failed_attempts.get(ip, []) if now - t < 300.0]
            self._failed_attempts[ip] = attempts
            return len(attempts) >= 8

    def record_failure(self, environ: Dict[str, Any]) -> None:
        ip = self._client_ip(environ)
        now = time.time()
        with self._fail_lock:
            if len(self._failed_attempts) > 500:
                self._failed_attempts = {
                    k: [t for t in v if now - t < 300.0]
                    for k, v in self._failed_attempts.items()
                }
                self._failed_attempts = {k: v for k, v in self._failed_attempts.items() if v}
            attempts = [t for t in self._failed_attempts.get(ip, []) if now - t < 300.0]
            attempts.append(now)
            self._failed_attempts[ip] = attempts

    def clear_failures(self, environ: Dict[str, Any]) -> None:
        ip = self._client_ip(environ)
        with self._fail_lock:
            self._failed_attempts.pop(ip, None)

    def issue_token(self, user: str, ttl_seconds: int) -> str:
        exp = int(time.time()) + max(300, int(ttl_seconds))
        payload = json.dumps({"u": user, "exp": exp}, separators=(",", ":")).encode("utf-8")
        b64 = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        sig = hmac.new(self.secret, b64.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{b64}.{sig}"

    def verify_token(self, token: str) -> Optional[str]:
        if not token or "." not in token:
            return None
        try:
            b64, sig = token.rsplit(".", 1)
            expected = hmac.new(self.secret, b64.encode("ascii"), hashlib.sha256).hexdigest()
            if not self._safe_eq(sig, expected):
                return None
            pad = "=" * ((4 - len(b64) % 4) % 4)
            data = json.loads(base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("utf-8"))
            if int(data.get("exp", 0)) < int(time.time()):
                return None
            u = str(data.get("u", ""))
            if self.enabled and not self._safe_eq(u, self.username):
                return None
            return u
        except Exception:
            return None

    def authenticated_user(self, environ: Dict[str, Any]) -> Optional[str]:
        if not self.enabled:
            return "local"
        raw_cookie = environ.get("HTTP_COOKIE", "")
        if not raw_cookie:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            return None
        morsel = jar.get(self.COOKIE_NAME)
        if not morsel:
            return None
        return self.verify_token(morsel.value)

    def build_cookie_header(self, token: str, max_age: int, environ: Dict[str, Any]) -> Tuple[str, str]:
        is_https = str(environ.get("HTTP_X_FORWARDED_PROTO", "")).lower() == "https" or environ.get("wsgi.url_scheme") == "https"
        parts = [
            f"{self.COOKIE_NAME}={token}",
            "Path=/",
            f"Max-Age={int(max_age)}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if is_https:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))


def create_wsgi_app(state: ControlCenterState):
    html_file = BASE_DIR / "dashboard_report.html"
    login_file = BASE_DIR / "login.html"
    logo_file = BASE_DIR / "kairos_logo.svg"
    js_file = BASE_DIR / "dashboard_app.js"
    auth = KairosSessionAuth()

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
        if method == "GET" and path in ("/favicon.svg", "/kairos-logo.svg"):
            body = logo_file.read_bytes() if logo_file.exists() else b""
            start_response("200 OK", headers + [("Content-Type", "image/svg+xml; charset=utf-8")])
            return [body]
        if method == "GET" and path == "/login":
            if auth.enabled and auth.authenticated_user(environ):
                start_response("302 Found", headers + [("Location", "/")])
                return [b""]
            body = login_file.read_bytes() if login_file.exists() else b"<h1>KAIROS Login</h1>"
            start_response("200 OK", headers + [("Content-Type", "text/html; charset=utf-8")])
            return [body]
        if method == "GET" and path == "/api/auth/status":
            user = auth.authenticated_user(environ)
            ver = getattr(state, "release_manifest", {}).get("version", "")
            payload = json.dumps({
                "ok": True,
                "auth_enabled": auth.enabled,
                "authenticated": bool(user),
                "user": user or "",
                "version": ver,
            }, ensure_ascii=False).encode("utf-8")
            start_response("200 OK", headers + [("Content-Type", "application/json; charset=utf-8")])
            return [payload]

        # Проверка авторизации для защищённых маршрутов (активна, если заданы KAIROS_AUTH_USER и KAIROS_AUTH_PASSWORD)
        is_public_auth_post = (method == "POST" and path in ("/api/auth/login", "/api/auth/logout"))
        if auth.enabled and not is_public_auth_post and not auth.authenticated_user(environ):
            if path.startswith("/api/") or method != "GET":
                start_response("401 Unauthorized", headers + [("Content-Type", "application/json; charset=utf-8")])
                return [json.dumps({"ok": False, "error": "unauthorized", "retMsg": "Требуется авторизация", "redirect": "/login"}, ensure_ascii=False).encode("utf-8")]
            qs_raw = environ.get("QUERY_STRING", "")
            next_target = path + ("?" + qs_raw if qs_raw else "")
            loc = "/login?next=" + quote(next_target, safe="/?=&")
            start_response("302 Found", headers + [("Location", loc)])
            return [b""]

        if method == "GET" and path in ("/", "/index.html"):
            body = html_file.read_bytes() if html_file.exists() else b"<h1>Loading...</h1>"
            start_response("200 OK", headers + [("Content-Type", "text/html; charset=utf-8")])
            return [body]
        if method == "GET" and path == "/dashboard.js":
            body = js_file.read_bytes() if js_file.exists() else b""
            start_response("200 OK", headers + [("Content-Type", "application/javascript; charset=utf-8")])
            return [body]
        if method == "GET" and path == "/api/state":
            payload = json.dumps(state.cached_state(), ensure_ascii=False).encode("utf-8")
            start_response("200 OK", headers + [("Content-Type", "application/json; charset=utf-8")])
            return [payload]
        if method == "GET" and path == "/api/strategy_detail":
            qs = parse_qs(environ.get("QUERY_STRING", ""))
            sid = (qs.get("id") or [""])[0].strip()
            if not sid:
                start_response("400 Bad Request", headers + [("Content-Type", "application/json; charset=utf-8")])
                return [b'{"ok":false,"retMsg":"Missing id"}']
            payload = json.dumps(state.get_strategy_detail(sid), ensure_ascii=False).encode("utf-8")
            start_response("200 OK", headers + [("Content-Type", "application/json; charset=utf-8")])
            return [payload]
        if method == "POST":
            if environ.get("HTTP_X_REQUESTED_WITH") not in ("AItrade", "KAIROS"): raise ValueError("Требуется заголовок клиента")
            origin=environ.get("HTTP_ORIGIN")
            if origin and urlsplit(origin).netloc != environ.get("HTTP_HOST"):raise ValueError("Недопустимый Origin")
            if environ.get("CONTENT_TYPE", "").split(";",1)[0] != "application/json":raise ValueError("Требуется application/json")
            length = int(environ.get("CONTENT_LENGTH", 0) or 0)
            if length<0 or length>65536:raise ValueError("Недопустимый размер запроса")
            raw = environ["wsgi.input"].read(length).decode("utf-8") if length > 0 else "{}"
            data = json.loads(raw or "{}")
            if not isinstance(data,dict):raise ValueError("Ожидается объект JSON")
            if path == "/api/auth/login":
                if auth.is_rate_limited(environ):
                    start_response("429 Too Many Requests", headers + [("Content-Type", "application/json; charset=utf-8")])
                    return [json.dumps({"ok": False, "retMsg": "Слишком много попыток входа. Повторите через 5 минут."}, ensure_ascii=False).encode("utf-8")]
                u_in = str(data.get("username", "") or "").strip()
                p_in = str(data.get("password", "") or "")
                if not auth.verify_credentials(u_in, p_in):
                    auth.record_failure(environ)
                    start_response("401 Unauthorized", headers + [("Content-Type", "application/json; charset=utf-8")])
                    return [json.dumps({"ok": False, "retMsg": "Неверный логин или пароль."}, ensure_ascii=False).encode("utf-8")]
                auth.clear_failures(environ)
                remember = bool(data.get("remember", True))
                ttl_h = int(os.getenv("KAIROS_SESSION_TTL_HOURS", "168") or 168) if remember else 24
                ttl_s = max(3600, ttl_h * 3600)
                tok = auth.issue_token(u_in or "local", ttl_s)
                nxt = str(data.get("next", "/") or "/")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = "/"
                cookie_hdr = auth.build_cookie_header(tok, ttl_s, environ)
                payload = json.dumps({"ok": True, "redirect": nxt}, ensure_ascii=False).encode("utf-8")
                start_response("200 OK", headers + [cookie_hdr, ("Content-Type", "application/json; charset=utf-8")])
                return [payload]
            if path == "/api/auth/logout":
                cookie_hdr = auth.build_cookie_header("", 0, environ)
                payload = json.dumps({"ok": True, "redirect": "/login"}, ensure_ascii=False).encode("utf-8")
                start_response("200 OK", headers + [cookie_hdr, ("Content-Type", "application/json; charset=utf-8")])
                return [payload]
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
                elif act == "save_subaccount_keys":
                    acc = str(data.get("account", "booster") or "booster").strip().lower()
                    t_mode = data.get("target_mode", "demo")
                    k_val = str(data.get("api_key", "") or "").strip()
                    s_val = str(data.get("api_secret", "") or "").strip()
                    resp = state.env_manager.save_subaccount_keys(acc, t_mode, k_val, s_val)
                    b_creds = state.env_manager.get_account_credentials("booster")
                    state.booster.state["isolated_subaccount"] = bool(b_creds.get("isolated", False))
                    if b_creds.get("isolated") and b_creds.get("api_key"):
                        if getattr(state, "booster_bybit", state.bybit) is state.bybit:
                            state.booster_bybit = BybitV5DirectController(state.engine)
                            state.booster_bybit.ledger = state.booster_ledger
                            state.booster_bybit.spm.ledger = state.booster_ledger
                            state.booster_bybit.spm.guard_path = BASE_DIR / "data" / "position_guard_booster.json"
                            state.booster_bybit.spm.breakeven_trigger_pct = state.bybit.spm.breakeven_trigger_pct
                            state.booster_bybit.spm.trailing_step_pct = state.bybit.spm.trailing_step_pct
                            state.booster_bybit.owner = state
                            b_chaser = SmartLimitChaser(state.booster_bybit.spm, state.ws_hub, BASE_DIR / "data")
                            b_chaser.enabled = state.limit_chaser.enabled
                            b_chaser.strict_post_only = state.limit_chaser.strict_post_only
                            state.booster_bybit.attach_chaser(b_chaser)
                        state.booster_bybit.apply_credentials(b_creds["api_key"], b_creds["api_secret"], b_creds["base_url"], sync_engine_client=False)
                    a_creds = state.env_manager.get_account_credentials("autotrade")
                    if a_creds.get("isolated") and a_creds.get("api_key"):
                        state.bybit.ledger = state.autotrade_ledger
                        state.bybit.spm.ledger = state.autotrade_ledger
                        state.bybit.spm.guard_path = BASE_DIR / "data" / "position_guard_autotrade.json"
                        state.bybit.apply_credentials(a_creds["api_key"], a_creds["api_secret"], a_creds["base_url"])
                    state.add_log("🔐 Ключи субаккаунта [" + acc.upper() + "/" + t_mode.upper() + "] сохранены (" + str(resp.get("masked_key")) + ").")
                elif act == "run_checklist":
                    t_mode = data.get("target_mode", "mainnet")
                    chk = state.env_manager.run_preflight_checklist(t_mode)
                    state.add_log("📋 Предторговый чек-лист [" + t_mode.upper() + "]: " + str(chk.get("summary")))
                    resp = {"ok": chk.get("passed", False), "checklist": chk, "retMsg": chk.get("summary")}
                elif act == "switch_mode":
                    t_mode = data.get("target_mode", "demo")
                    if t_mode != "demo":raise ValueError("Mainnet отключён до отдельной валидации")
                    state.entry_paused.set()
                    state.entry_pause_reason = "Переключение контура биржи"
                    state.entry_paused_at = time.time()
                    state._entry_pause_source = "user"
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
                cal_res = state.ml_calibrator.calibrate_from_closed_pnl(state._get_all_closed_pnl())
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

                guard_blocked, guard_block_reason = (
                    state.is_guard_pause_blocking()
                    if hasattr(state, "is_guard_pause_blocking")
                    else (bool((getattr(state, "guard_status", {}) or {}).get("last_error")), "активна ошибка Smart Guard")
                )
                emg_report = None
                ret_msg = ""
                if mode == "toggle_booster":
                    state.autopilot_booster_enabled = not state.autopilot_booster_enabled
                    if state.autopilot_booster_enabled:
                        state._last_auto_ts = 0.0
                        if not guard_blocked:
                            state.entry_paused.clear()
                            state.entry_pause_reason = ""
                            state._entry_pause_source = ""
                        else:
                            ret_msg = f"Автопилот включён, но защитная пауза Smart Guard сохранена: {guard_block_reason}"
                    elif not state.autopilot_core_enabled:
                        state.entry_paused.set()
                        if not guard_blocked:
                            state.entry_pause_reason = "Автопилоты выключены пользователем"
                            state.entry_paused_at = time.time()
                            state._entry_pause_source = "user"
                    state.add_log("🚀 Автопилот Разгона: " + ("ВКЛЮЧЕН" if state.autopilot_booster_enabled else "ВЫКЛЮЧЕН"))
                elif mode == "toggle_core":
                    state.autopilot_core_enabled = not state.autopilot_core_enabled
                    if state.autopilot_core_enabled:
                        state._last_auto_ts = 0.0
                        if not guard_blocked:
                            state.entry_paused.clear()
                            state.entry_pause_reason = ""
                            state._entry_pause_source = ""
                        else:
                            ret_msg = f"Автопилот включён, но защитная пауза Smart Guard сохранена: {guard_block_reason}"
                    elif not state.autopilot_booster_enabled:
                        state.entry_paused.set()
                        if not guard_blocked:
                            state.entry_pause_reason = "Автопилоты выключены пользователем"
                            state.entry_paused_at = time.time()
                            state._entry_pause_source = "user"
                    state.add_log("🎯 Автотрейдинг Core: " + ("ВКЛЮЧЕН" if state.autopilot_core_enabled else "ВЫКЛЮЧЕН"))
                elif mode == "enable_both":
                    state.autopilot_booster_enabled = True
                    state.autopilot_core_enabled = True
                    state._last_auto_ts = 0.0
                    if not guard_blocked:
                        state.entry_paused.clear()
                        state.entry_pause_reason = ""
                        state._entry_pause_source = ""
                    else:
                        ret_msg = f"Автопилоты включены, но защитная пауза Smart Guard сохранена: {guard_block_reason}"
                    state.add_log("⚡ Включены ОБА автопилота: Разгон + Core!")
                elif mode == "stop_all":
                    state.autopilot_booster_enabled = False
                    state.autopilot_core_enabled = False
                    state.entry_paused.set()
                    if not guard_blocked:
                        state.entry_pause_reason = "Остановлено пользователем"
                        state.entry_paused_at = time.time()
                        state._entry_pause_source = "user"
                    state.add_log("⏸ ВСЕ автопилоты остановлены пользователем. Открытые позиции под защитой Smart Guard.")
                elif mode == "emergency_stop":
                    emg_report = state.execute_emergency_stop()

                state.save_persisted_state()
                if emg_report is not None:
                    resp = emg_report
                else:
                    resp = {
                        "ok": True,
                        "booster_enabled": state.autopilot_booster_enabled,
                        "core_enabled": state.autopilot_core_enabled,
                        "entry_paused": state.entry_paused.is_set(),
                        "entry_pause_reason": getattr(state, "entry_pause_reason", ""),
                    }
                    if ret_msg:
                        resp["retMsg"] = ret_msg
            elif path == "/api/booster_config":
                guard_blocked, guard_block_reason = (
                    state.is_guard_pause_blocking()
                    if hasattr(state, "is_guard_pause_blocking")
                    else (bool((getattr(state, "guard_status", {}) or {}).get("last_error")), "активна ошибка Smart Guard")
                )
                if data.get("reset"):
                    state.booster.reset_challenge(float(data.get("start_equity", 1000.0)), float(data.get("target_equity", 10000.0)))
                    for ctrl in state._active_controllers():
                        ctrl.reconcile_and_clear_unresolved()
                    if (state.autopilot_booster_enabled or state.autopilot_core_enabled) and not guard_blocked:
                        state.entry_paused.clear()
                        state.entry_pause_reason = ""
                        state._entry_pause_source = ""
                    elif guard_blocked:
                        resp["retMsg"] = f"Сброс выполнен, но защитная пауза Smart Guard сохранена: {guard_block_reason}"
                    state.refresh_booster_snapshot()
                    state.add_log("🚀 ПОЛНЫЙ СБРОС РАЗГОНА: Баланс $1,000.00, дневной стоп-кран и все кулдауны монет полностью очищены!")
                elif data.get("unlock_limits"):
                    state.booster.unlock_limits_only()
                    for ctrl in state._active_controllers():
                        ctrl.reconcile_and_clear_unresolved()
                    if (state.autopilot_booster_enabled or state.autopilot_core_enabled) and not guard_blocked:
                        state.entry_paused.clear()
                        state.entry_pause_reason = ""
                        state._entry_pause_source = ""
                    elif guard_blocked:
                        resp["retMsg"] = f"Лимиты сняты, но защитная пауза Smart Guard сохранена: {guard_block_reason}"
                    state.refresh_booster_snapshot()
                    state.add_log("🔓 Дневной стоп-кран (-3%), счётчик дневных входов и кулдауны монет сняты! Торговля продолжается с текущим балансом.")
                elif "training_mode" in data:
                    t_on = not bool(state.booster.state.get("training_unlimited_mode", False))
                    state.booster.toggle_training_mode(t_on)
                    if t_on:
                        for ctrl in state._active_controllers():
                            ctrl.reconcile_and_clear_unresolved()
                        if (state.autopilot_booster_enabled or state.autopilot_core_enabled) and not guard_blocked:
                            state.entry_paused.clear()
                            state.entry_pause_reason = ""
                            state._entry_pause_source = ""
                        elif guard_blocked:
                            resp["retMsg"] = f"Режим обучения включён, но защитная пауза Smart Guard сохранена: {guard_block_reason}"
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
                b_ctrl = getattr(state, "booster_bybit", state.bybit)
                b_metrics = state.booster.compute_metrics(b_ctrl.get_closed_pnl(), b_ctrl.get_positions())
                demo_lab_cfg_m = state.settings.get("demo_lab", {}) if isinstance(state.settings, dict) else {}
                demo_lab_m = bool(demo_lab_cfg_m.get("enabled", False) and b_ctrl.base_url == "https://api-demo.bybit.com")
                sizing = state.booster.calculate_order_qty(entry_px, sl_px, state._demo_lab_booster_sizing_metrics(b_metrics, demo_lab_m))
                resp = b_ctrl.place_order(
                    sym, side, "Market", sizing["qty_coins"], entry_px, tp_px, sl_px, sizing["leverage"], state.split_tp_enabled, strategy_id="BOOSTER:MANUAL"
                )
                if resp.get("ok"):
                    state.mark_position_source(sym, "🚀 Booster A+")
                    state.booster.register_booster_symbol(sym)
                    state.add_log("🚀 Booster-сделка [" + str(resp.get("exec_mode", "MAKER")) + "]: " + sym + " " + side + " qty=" + str(resp.get("qty")) + " (Риск $" + str(sizing["risk_usd"]) + ")")
            elif path == "/api/smart_guard":
                state.smart_guard_enabled = bool(data.get("enabled", True))
                state.split_tp_enabled = bool(data.get("split_tp_enabled", True))
                if "limit_chaser_enabled" in data:
                    state.limit_chaser.enabled = bool(data.get("limit_chaser_enabled", True))
                if "limit_chaser_strict" in data:
                    state.limit_chaser.strict_post_only = bool(data.get("limit_chaser_strict", False))
                be_val = float(data.get("breakeven_trigger_pct", 0.55) or 0.55)
                tr_val = float(data.get("trailing_step_pct", 0.40) or 0.40)
                for ctrl in state._active_controllers():
                    ctrl.spm.breakeven_trigger_pct = be_val
                    ctrl.spm.trailing_step_pct = tr_val
                    c_ch = getattr(ctrl, "chaser", None)
                    if c_ch is not None:
                        c_ch.enabled = state.limit_chaser.enabled
                        c_ch.strict_post_only = state.limit_chaser.strict_post_only
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
                ctrl = getattr(state, "booster_bybit", state.bybit) if str(data.get("account", "")).lower() == "booster" else state.bybit
                avg_px = float(data.get("avg_price", 0) or 0)
                cur_tp = float(data.get("take_profit", 0) or 0)
                tp1_px = avg_px + 0.5 * (cur_tp - avg_px) if cur_tp > 0 else (avg_px * 1.008 if data["side"] == "Buy" else avg_px * 0.992)
                resp = ctrl.spm.place_tp1_half_limit(data["symbol"], data["side"], float(data.get("size", 0) or 0), tp1_px, int(data.get("position_idx", 0) or 0))
                state.add_log("🎯 Выставлен TP1 50% для " + str(data["symbol"]) + " @ " + str(resp.get("tp1_price")))
            elif path == "/api/pos_close_half":
                ctrl = getattr(state, "booster_bybit", state.bybit) if str(data.get("account", "")).lower() == "booster" else state.bybit
                resp = ctrl.close_half_position(data["symbol"], data["side"], str(data["size"]), int(data.get("position_idx", 0) or 0))
                state.add_log("✂️ Отправлена заявка на закрытие 50% позиции " + str(data["symbol"]))
            elif path == "/api/pos_breakeven":
                ctrl = getattr(state, "booster_bybit", state.bybit) if str(data.get("account", "")).lower() == "booster" else state.bybit
                resp = ctrl.spm.move_sl_to_breakeven(data["symbol"], data["side"], float(data.get("avg_price", 0) or 0), int(data.get("position_idx", 0) or 0))
                state.add_log("🛡 Стоп-лосс " + str(data["symbol"]) + " переведен в Безубыток (" + str(resp.get("be_price")) + ")")
            elif path == "/api/pos_trailing":
                ctrl = getattr(state, "booster_bybit", state.bybit) if str(data.get("account", "")).lower() == "booster" else state.bybit
                resp = ctrl.spm.activate_trailing_stop(data["symbol"], float(data.get("mark_price", 0) or 0), ctrl.spm.trailing_step_pct, int(data.get("position_idx", 0) or 0))
                state.add_log("📈 Включен Trailing Stop (" + str(resp.get("trailing_dist")) + ") для " + str(data["symbol"]))
            elif path == "/api/close_position":
                ctrl = getattr(state, "booster_bybit", state.bybit) if str(data.get("account", "")).lower() == "booster" else state.bybit
                resp = ctrl.close_position(data["symbol"], data["side"], str(data["size"]), int(data.get("position_idx", 0) or 0))
                state.add_log("✖ Отправлена заявка на закрытие позиции " + str(data["symbol"]))
            elif path == "/api/close_all_positions":
                res_list = [ctrl.close_all_positions() for ctrl in state._active_controllers()]
                resp = {"ok": all(r.get("ok") for r in res_list), "submitted_count": sum(int(r.get("submitted_count", 0)) for r in res_list)}
                state.add_log("🚨 Отправлены заявки на закрытие позиций на все активные контуры.")
            elif path == "/api/cancel_order":
                ctrl = getattr(state, "booster_bybit", state.bybit) if str(data.get("account", "")).lower() == "booster" else state.bybit
                resp = ctrl.cancel_order(data["symbol"], data["order_id"])
            elif path == "/api/cancel_all_orders":
                res_list = [ctrl.cancel_all_orders() for ctrl in state._active_controllers()]
                resp = {"ok": all(r.get("ok") for r in res_list)}
                state.add_log("🗑 Запрос отмены активных ордеров отправлен на все активные контуры.")

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
        except ValueError as exc:
            state.add_log("Ошибка валидации HTTP-команды: " + str(exc)[:200])
            start_response("400 Bad Request",[("Content-Type","application/json; charset=utf-8"),("Cache-Control","no-store")])
            return [json.dumps({"ok":False,"retMsg":str(exc)[:200]},ensure_ascii=False).encode()]
        except Exception as exc:
            state.add_log("Внутренняя ошибка HTTP-команды: " + type(exc).__name__ + ": " + str(exc)[:200])
            start_response("500 Internal Server Error",[("Content-Type","application/json; charset=utf-8"),("Cache-Control","no-store")])
            return [json.dumps({"ok":False,"retMsg":"Внутренняя ошибка сервера"},ensure_ascii=False).encode()]
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

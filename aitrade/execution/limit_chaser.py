"""Execution uses exchange-confirmed quantities and cancellation, never inferred fills."""
import json,time,uuid
from aitrade.storage import atomic_json
from aitrade.exchange.transport import ExchangeError
class SmartLimitChaser:
    def __init__(self,spm,ws_hub,data_dir):
        self.spm=spm;self.ws_hub=ws_hub;self.stats_path=data_dir/'chaser_stats.json'
        self.enabled=True;self.strict_post_only=False;self.max_attempts=2;self.step_wait_sec=1.5;self.max_chase_drift_pct=.60
        self.stats=json.loads(self.stats_path.read_text()) if self.stats_path.exists() else {'total_orders':0,'maker_filled':0,'fallback_taker_filled':0,'fee_saved_usd':0.,'total_notional_usd':0.}
    def _get_live_bbo(self,symbol,fallback_px):
        try:
            return self.ws_hub.get_bbo(symbol,max_age=30)
        except Exception:
            if fallback_px and float(fallback_px) > 0:
                px = float(fallback_px)
                return px * 0.9999, px * 1.0001
            raise
    def _status(self,symbol,order_id):
        for path in ('/v5/order/realtime','/v5/order/history'):
            rows=self.spm.signed_get(path,{'category':'linear','symbol':symbol,'orderId':order_id})['result']['list']
            if rows:return rows[0]
        raise ExchangeError('Статус заявки не подтвержден',uncertain=True)
    def execute_chased_entry(self,symbol,side,qty,ref_price,take_profit=None,stop_loss=None,leverage=3,enable_split_tp=True,order_link_id=None,position_idx=0):
        spec=self.spm.get_spec(symbol);qs=self.spm.fmt_step(qty,spec['qty_step'],True);total=float(qs)
        bid,ask=self._get_live_bbo(symbol,ref_price);price=bid if side=='Buy' else ask
        if abs(price/ref_price-1)*100>self.max_chase_drift_pct:raise ExchangeError('Цена ушла от сигнала')
        effective_link_id = order_link_id or ('at-' + uuid.uuid4().hex[:30])
        payload={'category':'linear','symbol':symbol,'side':side,'orderType':'Limit','qty':qs,'price':self.spm.fmt_step(price,spec['tick_size'],side=='Buy'),'timeInForce':'PostOnly','positionIdx':position_idx,'orderLinkId':effective_link_id, 'takeProfit':self.spm.fmt_step(take_profit,spec['tick_size'],False),'stopLoss':self.spm.fmt_step(stop_loss,spec['tick_size'],False),'tpslMode':'Full'}
        if getattr(self.spm,'ledger',None):self.spm.ledger.prepare_order(payload,effective_link_id)
        r=self.spm.signed_post('/v5/order/create',payload);oid=r['result']['orderId'];status=None
        for _ in range(self.max_attempts):
            time.sleep(self.step_wait_sec);status=self._status(symbol,oid)
            if status['orderStatus'] in ('Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated'):break
        terminal=('Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated')
        if status['orderStatus'] not in terminal:
            try:self.spm.signed_post('/v5/order/cancel',{'category':'linear','symbol':symbol,'orderId':oid})
            except ExchangeError as e:
                if e.uncertain:raise
            for _ in range(6):
                time.sleep(.3);status=self._status(symbol,oid)
                if status['orderStatus'] in terminal:break
            else:raise ExchangeError('Отмена не подтверждена; повторный вход запрещён',uncertain=True)
        filled=float(status.get('cumExecQty') or 0);rem=max(0,total-filled);used_market=False;ids=[oid]
        if rem>=spec['min_qty'] and not self.strict_post_only:
            bid,ask=self._get_live_bbo(symbol,ref_price);market_px=ask if side=='Buy' else bid
            # Revalidate TP/SL side and drift before fallback; quantity never increases.
            valid=(stop_loss<market_px<take_profit if side=='Buy' else take_profit<market_px<stop_loss)
            if valid and abs(market_px/ref_price-1)*100<=self.max_chase_drift_pct:
                fb=dict(payload,orderType='Limit',timeInForce='IOC',qty=self.spm.fmt_step(rem,spec['qty_step'],True),orderLinkId=effective_link_id[:30]+'-m')
                cap=market_px*(1.0015 if side=='Buy' else .9985)
                if not (stop_loss<cap<take_profit if side=='Buy' else take_profit<cap<stop_loss):
                    if filled>0:
                        pass
                    else:
                        raise ExchangeError('IOC price cap outside TP/SL',uncertain=False)
                else:
                    fb['price']=self.spm.fmt_step(cap,spec['tick_size'],side=='Buy')
                    if getattr(self.spm,'ledger',None):self.spm.ledger.prepare_order(fb,effective_link_id)
                    res=self.spm.signed_post('/v5/order/create',fb);fid=res['result']['orderId'];ids.append(fid)
                    for _ in range(8):
                        time.sleep(.3);fs=self._status(symbol,fid)
                        if fs['orderStatus'] in terminal:break
                    else:raise ExchangeError('Рыночное исполнение не подтверждено',uncertain=True)
                    filled+=float(fs.get('cumExecQty') or 0);used_market=True
        self.stats['total_orders']=int(self.stats.get('total_orders',0))+1
        self.stats['fallback_taker_filled' if used_market else 'maker_filled']=int(self.stats.get('fallback_taker_filled' if used_market else 'maker_filled',0))+(1 if filled>0 else 0)
        atomic_json(self.stats_path,self.stats)
        return {'ok':filled>0,'status':('FILLED' if filled>=total-1e-10 else 'PARTIAL') if filled>0 else 'REJECTED','qty':str(filled),'orderId':ids[-1],'orderIds':ids,'symbol':symbol,'side':side,'exec_mode':'LIMIT_WITH_CONFIRMED_FILLS','retMsg':'Подтверждено биржей' if filled>0 else 'Заявка не исполнена'}
    def get_summary(self):
        tot=int(self.stats.get('total_orders',0));mk=int(self.stats.get('maker_filled',0))
        return dict(self.stats,enabled=self.enabled,strict_post_only=self.strict_post_only,maker_rate_pct=round(mk/tot*100,1) if tot else None,statistics_note='Счётчики заявок; точные комиссии и maker/taker — в журнале исполнений')

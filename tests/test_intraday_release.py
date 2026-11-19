import unittest,tempfile,json,time,threading,random
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace as NS
from collections import deque
from aitrade.storage import Ledger
from aitrade.dashboard.live_server import BybitV5DirectController,ControlCenterState
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.exchange.transport import ExchangeError
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.exchange.bybit_v5 import Candle
from aitrade.strategies.regime_s1_s3 import aggregate_1h,_build_ml_features,classify_regime_1h
from aitrade.features.indicators import calc_session_vwap_bands
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.booster.x_news_radar import XNewsSentimentRadar
from aitrade.booster.deposit_booster import DepositBoosterManager

class ReleaseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name);self.ledger=Ledger(self.path/'ledger.db')
 def tearDown(self):self.tmp.cleanup()
 def intent(self,i='entry',strategy='CORE:S1_TREND_RETEST'):
  self.ledger.intent(i,'BTCUSDT',strategy,5,{'side':'Buy','qty':10,'orderId':i});self.ledger.finish(i,'FILLED',{})
 def fill(self,eid,side,qty,closed,ts,oid=None,link=''):
  self.ledger.put_records('executions',[dict(execId=eid,orderId=oid or eid,orderLinkId=link,symbol='BTCUSDT',side=side,execQty=str(qty),closedSize=str(closed),execTime=str(ts),execType='Trade')],'execId')
 def test_partial_closes_are_quantity_matched_independent_of_row_order(self):
  self.intent();self.fill('fill','Buy',10,0,1,'entry');self.fill('c1','Sell',4,4,2);self.fill('c2','Sell',6,6,3)
  rows=[dict(orderId='c1',closedSize='4'),dict(orderId='c2',closedSize='6')]
  for sequence in [rows,rows[::-1]]:
   self.assertEqual([r['entry_intent_id'] for r in self.ledger.attribute(sequence)],['entry','entry'])
  self.assertIsNone(self.ledger.active_intent('BTCUSDT','Buy'))
 def test_external_reentry_does_not_inherit_old_strategy(self):
  self.intent();self.fill('f','Buy',10,0,1,'entry');self.fill('c','Sell',10,10,2);self.fill('external','Buy',10,0,3)
  self.assertEqual(self.ledger.latest_strategy('BTCUSDT','Buy'),'UNATTRIBUTED')
 def test_mixed_position_not_guessed_fifo(self):
  self.intent();self.fill('f','Buy',10,0,1,'entry');self.fill('outside','Buy',5,0,2);self.fill('c','Sell',3,3,3)
  self.assertEqual(self.ledger.attribute([{'orderId':'c','closedSize':3}])[0]['strategy_id'],'UNATTRIBUTED')
 def test_missing_open_history_unattributed(self):
  self.intent();self.fill('c','Sell',10,10,1)
  self.assertEqual(self.ledger.attribute([{'orderId':'c','closedSize':10}])[0]['strategy_id'],'UNATTRIBUTED')
 def test_live_size_must_match_remaining_fills(self):
  self.intent();self.fill('f','Buy',10,0,1,'entry')
  self.assertIsNone(self.ledger.active_intent('BTCUSDT','Buy',9))
  self.assertIsNotNone(self.ledger.active_intent('BTCUSDT','Buy',10))
 def controller(self):
  c=BybitV5DirectController.__new__(BybitV5DirectController);c.ledger=self.ledger;c._entry_lock=threading.RLock();c.spm=NS();return c
 def test_unknown_child_survives_parent_cancellation_and_restart(self):
  self.intent();self.ledger.finish('entry','UNCERTAIN',{})
  self.ledger.prepare_order({'symbol':'BTCUSDT','orderLinkId':'entry'},'entry')
  self.ledger.prepare_order({'symbol':'BTCUSDT','orderLinkId':'child'},'entry')
  c=self.controller();c.ledger=Ledger(self.path/'ledger.db')
  c.spm.signed_get=lambda path,q:{'result':{'list':[{'orderId':'parent','orderStatus':'Cancelled','cumExecQty':'0'}] if q.get('orderLinkId')=='entry' else []}}
  c.reconcile_and_clear_unresolved();self.assertEqual(c.ledger.unresolved(),[('entry',)])
 def test_parent_and_child_partial_sum(self):
  self.intent();self.ledger.finish('entry','UNCERTAIN',{})
  for link in ['entry','child']:self.ledger.prepare_order({'symbol':'BTCUSDT','orderLinkId':link},'entry')
  c=self.controller();c.spm.signed_get=lambda path,q:{'result':{'list':[{'orderId':q.get('orderLinkId'),'orderStatus':'Cancelled','cumExecQty':'4' if q.get('orderLinkId')=='entry' else '6'}]}}
  c.reconcile_pending();self.assertEqual(self.ledger.unresolved(),[])
  with self.ledger.connect() as db:
   st,p=db.execute('SELECT status,payload FROM intents').fetchone();self.assertEqual(st,'FILLED');self.assertEqual(json.loads(p)['confirmedQty'],10)
 def test_order_identity_committed_before_transport_timeout(self):
  sp=SmartPositionManager('dummy','dummy');sp.ledger=self.ledger
  sp.signed_get=Mock(return_value={'result':{'list':[]}})
  def fail(*a):
   with self.ledger.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],1)
   raise ExchangeError('timeout',uncertain=True)
  with patch('aitrade.execution.smart_position_manager.request',side_effect=fail),self.assertRaises(ExchangeError):sp.signed_post('/v5/order/create',{'symbol':'BTCUSDT','orderLinkId':'close','qty':'1','reduceOnly':True})
 def test_legacy_ambiguous_child_is_not_cleared(self):
  self.intent();self.ledger.finish('entry','UNCERTAIN',{'orderId':'','error':'child timeout'})
  c=self.controller();c.spm.signed_get=Mock(return_value={'result':{'list':[]}});c.reconcile_pending();self.assertTrue(c.ledger.unresolved())
 def test_duplicate_hour_rejected(self):
  bars=[Candle(t*60000,100,101,99,100,10,1000) for t in [0,15,15,45]];self.assertEqual(aggregate_1h(bars),[])
 def test_complete_hour_accepted(self):
  bars=[Candle(t*60000,100,101,99,100,10,1000) for t in [0,15,30,45]];self.assertEqual(len(aggregate_1h(bars)),1)
 def test_session_vwap_does_not_use_yesterday(self):
  bars=[Candle(86400000-900000,1000,1000,1000,1000,100,100000),Candle(86400000,100,100,100,100,1,100)]
  self.assertEqual(calc_session_vwap_bands(bars).vwap,100)
 def test_news_negative_in_next_clause_does_not_cancel_hack(self):
  r=XNewsSentimentRadar()
  for t in ['Exchange hacked, withdrawals not working.','Exchange hacked. No update yet.','Bitcoin not hacked. Later the exchange was hacked and funds stolen.']:self.assertLess(r._score_text(t)[0],0)
  self.assertEqual(r._score_text('Bitcoin was not hacked. Exploit prevented.')[0],0)
 def test_trade_dedup_more_than_2000(self):
  h=BybitV5WebSocketHub(['BTCUSDT'])
  with patch('aitrade.exchange.ws_stream.time.time',return_value=10000):
   for i in list(range(2100))+[0,10,2000]:h._handle_message(json.dumps({'topic':'publicTrade.BTCUSDT','data':[{'T':10000000,'i':str(i),'p':'100','v':'1','S':'Buy'}]}))
   self.assertEqual(h.get_symbol_metrics('BTCUSDT')['trades_count'],2100)
 def test_liquidation_duplicate_and_future_rejected(self):
  h=BybitV5WebSocketHub(['BTCUSDT'])
  with patch('aitrade.exchange.ws_stream.time.time',return_value=10000):
   for ts in [10000000,10000000,11000000]:h._handle_message(json.dumps({'topic':'allLiquidation.BTCUSDT','data':[{'T':ts,'S':'Buy','p':'100','v':'6'}]}))
   self.assertEqual(h.get_symbol_metrics('BTCUSDT')['long_liq_usd'],600)
 def test_out_of_order_old_events_pruned(self):
  q=deque([(10,1,True),(1,2,True),(8,3,False)]);BybitV5WebSocketHub._prune_deque(q,5);self.assertEqual(len(q),2)
 def test_flow_not_complete_on_connect(self):
  h=BybitV5WebSocketHub(['BTCUSDT']);h.connected=True;self.assertFalse(h.get_summary()['flow_window_complete'])
 def test_stale_health_not_green(self):
  s=ControlCenterState.__new__(ControlCenterState);s.scan_status={'running':False,'last_attempt':0,'failed_cycles':3,'last_error':'offline'};s._autopilot_thread=NS(is_alive=lambda:True);s.entry_paused=threading.Event();s._snapshot_ts=time.time()-7200;s._snapshot_error='offline';s.guard_status={'heartbeat':time.time(),'last_error':''}
  self.assertFalse(s.health()['ok']);self.assertTrue(s.health()['liveness_ok'])
 def test_guard_hang_is_liveness_failure(self):
  s=ControlCenterState.__new__(ControlCenterState);s.scan_status={'running':False};s._autopilot_thread=NS(is_alive=lambda:True);s.entry_paused=threading.Event();s._snapshot_ts=time.time();s._snapshot_error='';s.guard_status={'heartbeat':time.time()-300}
  self.assertFalse(s.health()['liveness_ok'])
 def bars(self,n=90):return [Candle(86400000+i*900000,100+i*.02,101+i*.02,99+i*.02,100+i*.02,10+i%13,1000) for i in range(n)]
 def test_train_and_live_feature_vector_identical(self):
  m=WalkForwardMetaLabeler({});bars=self.bars();X,_,_=m._extract_historical_matrix(bars,1)
  self.assertEqual(X[50-18],m._feature_vector(bars[:51]))
  f=_build_ml_features(bars[:51],obi_smoothed=.99);self.assertNotEqual(f['obi_smoothed'],X[50-18][-1])
 def test_label_returns_stop_not_last_close(self):
  m=WalkForwardMetaLabeler({});bars=[Candle(86400000+i*900000,100,100.1,99.9,100,10,1000) for i in range(50)]
  bars[19]=Candle(bars[19].timestamp,100,100,95,99,10,1000);bars[26]=Candle(bars[26].timestamp,100,110,100,110,10,1000)
  _,y,r=m._extract_historical_matrix(bars,1);self.assertEqual(y[0],0);self.assertLess(r[0],0)
 def test_replay_stop_precedes_target_and_gap_loss(self):
  b=WalkForwardBacktester({});bar=Candle(0,98,104,97,103,1,100)
  pnl,_,used=b._resolve_trade([bar],'Buy',100,99,103,0,0,0,False);self.assertEqual(pnl,-2);self.assertEqual(used,1)
 def test_portfolio_reserves_slots_until_exit(self):
  b=WalkForwardBacktester({});trades=[(0,1,1,'S1',900000,str(i),.01) for i in range(5)]
  pnls,equity,dd,skipped=b._portfolio(trades);self.assertEqual(len(pnls),3);self.assertEqual(skipped,2)
 def test_replay_has_no_synthetic_fallback_or_fake_sharpe(self):
  b=WalkForwardBacktester({});self.assertFalse(b.client.allow_offline_fallback)
  r=b._summarize('x',[(1,1,1,'S',2,'BTC',.01)]);self.assertIsNone(r.sharpe_ratio);self.assertIsNone(r.deflated_sharpe_ratio)
 def test_campaign_daily_reset_preserves_history(self):
  b=DepositBoosterManager(self.path);b.state['started_at_ms']=0;b.state['limits_reset_at_ms']=0
  rows=[{'updatedTime':int(time.time()*1000)-1000,'closedPnl':'-40','strategy_id':'BOOSTER:test'}]
  before=b.compute_metrics(rows,[]);self.assertTrue(before['circuit_breaker_tripped'])
  b.unlock_limits_only();after=b.compute_metrics(rows,[]);self.assertEqual(after['current_equity'],960);self.assertFalse(after['circuit_breaker_tripped'])
 def test_campaign_risk_cap_and_no_return_forecast(self):
  b=DepositBoosterManager(self.path);b.state['manual_adjustment_usd']=4000
  r=b.compute_metrics([],[]);self.assertLessEqual(r['effective_risk_usd'],5);self.assertIsNone(r['compound_steps_left']);self.assertFalse(r['auto_pyramid'])
 def test_campaign_target_is_terminal_when_flat(self):
  b=DepositBoosterManager(self.path);b.state['manual_adjustment_usd']=9000;r=b.compute_metrics([],[]);self.assertTrue(r['goal_reached']);self.assertEqual(r['campaign_status'],'completed')
 def test_old_campaign_exit_cannot_inflate_new_campaign(self):
  b=DepositBoosterManager(self.path);b.state['started_at_ms']=2000
  rows=[{'updatedTime':3000,'entry_created_ms':1000,'campaign_id':'1000','strategy_id':'BOOSTER:test','closedPnl':'500'}]
  self.assertEqual(b.compute_metrics(rows,[])['current_equity'],1000)
 def test_legacy_entry_before_reset_excluded(self):
  b=DepositBoosterManager(self.path);b.state['started_at_ms']=2000
  rows=[{'updatedTime':3000,'entry_created_ms':1000,'strategy_id':'BOOSTER:test','closedPnl':'500'}]
  self.assertEqual(b.compute_metrics(rows,[])['current_equity'],1000)
 def test_daily_reset_keeps_campaign_identity(self):
  b=DepositBoosterManager(self.path);before=b.compute_metrics([],[])['campaign_id'];b.unlock_limits_only()
  self.assertEqual(b.compute_metrics([],[])['campaign_id'],before)
 def test_tp1_partial_cancellation_resubmits_only_remaining_target(self):
  sp=SmartPositionManager('dummy','dummy');sp.guard_path=self.path/'guard.json';sp.ledger=NS(active_intent=lambda *a:{'intent_id':'entry','strategy_id':'BOOSTER:test','sl':99})
  pos={'symbol':'BTCUSDT','side':'Buy','positionIdx':0,'size':'10','avgPrice':'100','markPrice':'100','stopLoss':'99','takeProfit':'104'}
  sp.paged_get=lambda path,q:[pos] if path.endswith('/position/list') else []
  sp.place_tp1_half_limit=Mock(return_value={'ok':True,'orderId':'tp1'})
  sp.signed_get=lambda *a:{'result':{'list':[{'orderStatus':'Cancelled','cumExecQty':'2'}]}}
  sp.audit_and_protect_open_positions();pos['size']='8';sp.audit_and_protect_open_positions()
  self.assertEqual(sp.place_tp1_half_limit.call_args.kwargs['target_qty'],1.)
 def test_no_successful_scan_is_not_ready_with_fresh_snapshot(self):
  s=ControlCenterState.__new__(ControlCenterState);s.scan_status={'running':False,'last_success':time.time()-7200};s._autopilot_thread=NS(is_alive=lambda:True);s.entry_paused=threading.Event();s._snapshot_ts=time.time();s._snapshot_error='';s.guard_status={'heartbeat':time.time()};s.autopilot_booster_enabled=True
  self.assertFalse(s.health()['ok']);self.assertTrue(s.health()['liveness_ok'])
 def test_pending_close_survives_restart_without_duplicate_submit(self):
  self.ledger.prepare_order({'symbol':'BTCUSDT','orderLinkId':'close','orderType':'Market','reduceOnly':True,'positionIdx':0,'qty':'10'})
  c=self.controller();c.ledger=Ledger(self.path/'ledger.db');c.spm.signed_post=Mock()
  r=c.close_position('BTCUSDT','Buy','10');self.assertEqual(r['status'],'PENDING');c.spm.signed_post.assert_not_called()
 def test_exit_ack_becomes_confirmed_terminal(self):
  self.ledger.prepare_order({'symbol':'BTCUSDT','orderLinkId':'close','orderType':'Market','reduceOnly':True,'qty':'10'})
  c=self.controller();c.spm.signed_get=lambda *a:{'result':{'list':[{'orderId':'closed','orderStatus':'Filled','cumExecQty':'10'}]}}
  c.reconcile_pending();self.assertEqual(c.ledger.pending_exit_orders(),[])
 def test_sensitive_ledger_mode(self):self.assertEqual(self.ledger.path.stat().st_mode&0o777,0o600)

if __name__=='__main__':unittest.main()

import unittest,tempfile,pathlib,threading,time,json,io,copy
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from aitrade.dashboard.live_server import BybitV5DirectController,ControlCenterState,create_wsgi_app
from aitrade.execution.limit_chaser import SmartLimitChaser
from aitrade.execution.smart_position_manager import SmartPositionManager
from aitrade.exchange.transport import ExchangeError
from aitrade.exchange.ws_stream import BybitV5WebSocketHub
from aitrade.booster.deposit_booster import DepositBoosterManager
from aitrade.booster.ml_calibrator import AdaptiveMLCalibrator
from aitrade.storage import Ledger,atomic_json

class SafetyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=pathlib.Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def controller(self):
  c=BybitV5DirectController.__new__(BybitV5DirectController);c.base_url='https://api-demo.bybit.com';c._entry_lock=threading.RLock()
  c.engine=NS(settings={'universe':{'core_pairs':['BTCUSDT']}});c.ledger=Ledger(self.path/'ledger.db')
  c.owner=NS(entry_paused=threading.Event(),ws_hub=NS(get_bbo=lambda *a:(100.,100.01),get_summary=lambda:{}),regime_guard=NS(can_open_position=lambda *a:(True,'ok')))
  c.owner.funding_rates={'BTCUSDT':0.0};c.owner.funding_updated_at=time.time();c.owner.ws_hub.get_summary=lambda:{'flow_window_complete':True}
  c.get_positions=lambda:[];c.get_open_orders=lambda:[];c.get_account_summary=lambda:{'usdt_balance':1000.};c.get_closed_pnl=lambda:[]
  spec={'qty_step':.1,'min_qty':.1,'max_qty':1000.,'min_notional':5.,'tick_size':.01}
  c.spm=NS(get_spec=lambda _:spec,fmt_step=SmartPositionManager.fmt_step,signed_post=Mock(return_value={'retCode':0,'result':{'orderId':'o'}}),paged_get=lambda *a:[{'positionIdx':0}])
  c.chaser=NS(enabled=False);return c
 def enter(self,c,**kw):
  args=dict(symbol='BTCUSDT',side='Buy',order_type='Market',qty=10.,price=100.,take_profit=103.,stop_loss=99.,leverage=3)
  args.update(kw);return c.place_order(**args)
 def test_risk_caps_quantity(self):
  c=self.controller();r=self.enter(c);self.assertTrue(r['ok']);payload=c.spm.signed_post.call_args.args[1];self.assertLessEqual(float(payload['qty'])*1.26,5.);self.assertEqual(payload['side'],'Buy');self.assertIn('orderLinkId',payload)
 def test_wrong_stop_rejected(self):
  c=self.controller();self.assertFalse(self.enter(c,stop_loss=101.)['ok']);c.spm.signed_post.assert_not_called()
 def test_nan_rejected(self):
  c=self.controller();self.assertFalse(self.enter(c,qty=float('nan'))['ok']);c.spm.signed_post.assert_not_called()
 def test_missing_tp_rejected(self):
  c=self.controller();self.assertFalse(self.enter(c,take_profit=0)['ok'])
 def test_mainnet_rejected(self):
  c=self.controller();c.base_url='https://api.bybit.com';self.assertFalse(self.enter(c)['ok'])
 def test_daily_loss_blocks(self):
  c=self.controller();c.get_closed_pnl=lambda:[{'closedPnl':'-37','updatedTime':str(int(time.time()*1000))}];self.assertFalse(self.enter(c)['ok']);c.spm.signed_post.assert_not_called()
 def test_unresolved_blocks(self):
  c=self.controller();c.ledger.intent('old','BTCUSDT','CORE',5,{});self.assertFalse(self.enter(c)['ok'])
 def test_existing_order_blocks(self):
  c=self.controller();c.get_open_orders=lambda:[{'symbol':'BTCUSDT','reduceOnly':False}];self.assertFalse(self.enter(c)['ok'])
 def test_automatic_pause_blocks(self):
  c=self.controller();c.owner.entry_paused.set();self.assertFalse(self.enter(c,strategy_id='CORE:test')['ok'])
 def test_small_size_not_inflated(self):
  c=self.controller();self.assertFalse(self.enter(c,qty=.001)['ok'])
 def chaser(self,strict=False):
  spec={'qty_step':1.,'min_qty':1.,'tick_size':.1}
  spm=NS(get_spec=lambda _:spec,fmt_step=SmartPositionManager.fmt_step,signed_post=Mock(side_effect=lambda path,p:{'retCode':0,'result':{'orderId':'market' if p.get('timeInForce')=='IOC' else 'limit'}}))
  c=SmartLimitChaser(spm,NS(get_bbo=lambda *a,**k:(100.,100.01)),self.path);c.max_attempts=1;c.step_wait_sec=0;c.strict_post_only=strict;return c
 def test_partial_fallback_only_remaining(self):
  c=self.chaser();c._status=Mock(side_effect=[{'orderStatus':'PartiallyFilled','cumExecQty':'4'},{'orderStatus':'Cancelled','cumExecQty':'4'},{'orderStatus':'Filled','cumExecQty':'6'}])
  with patch('aitrade.execution.limit_chaser.time.sleep'):r=c.execute_chased_entry('BTCUSDT','Buy',10,100,103,99)
  payload=[a.args[1] for a in c.spm.signed_post.call_args_list if a.args[1].get('timeInForce')=='IOC'][0];self.assertEqual(float(payload['qty']),6);self.assertEqual(float(r['qty']),10)
 def test_cancel_race_no_second_entry(self):
  c=self.chaser();c._status=Mock(side_effect=[{'orderStatus':'PartiallyFilled','cumExecQty':'4'},{'orderStatus':'Filled','cumExecQty':'10'}])
  with patch('aitrade.execution.limit_chaser.time.sleep'):r=c.execute_chased_entry('BTCUSDT','Buy',10,100,103,99)
  self.assertFalse(any(x.args[1].get('timeInForce')=='IOC' for x in c.spm.signed_post.call_args_list));self.assertEqual(r['status'],'FILLED')
 def test_unknown_status_not_fill(self):
  c=self.chaser();c._status=Mock(side_effect=ExchangeError('timeout',uncertain=True))
  with patch('aitrade.execution.limit_chaser.time.sleep'),self.assertRaises(ExchangeError):c.execute_chased_entry('BTCUSDT','Buy',10,100,103,99)
  self.assertEqual(c.stats['maker_filled'],0)
 def test_strict_keeps_partial(self):
  c=self.chaser(True);c._status=Mock(side_effect=[{'orderStatus':'PartiallyFilled','cumExecQty':'4'},{'orderStatus':'Cancelled','cumExecQty':'4'}])
  with patch('aitrade.execution.limit_chaser.time.sleep'):r=c.execute_chased_entry('BTCUSDT','Buy',10,100,103,99)
  self.assertEqual(float(r['qty']),4);self.assertEqual(r['status'],'PARTIAL')
 def test_book_reads_actual_levels_and_age(self):
  h=BybitV5WebSocketHub(['BTCUSDT']);h.connected=True;h.orderbooks['BTCUSDT']={'bids':{99.:2},'asks':{101.:2}};h.book_timestamps['BTCUSDT']=time.time();self.assertEqual(h.get_bbo('BTCUSDT'),(99.,101.));h.book_timestamps['BTCUSDT']=0
  with self.assertRaises(RuntimeError):h.get_bbo('BTCUSDT')
 def test_full_loss_no_clip(self):
  b=DepositBoosterManager(self.path);r=b.compute_metrics([{'symbol':'BTCUSDT','closedPnl':'-100','updatedTime':int(time.time()*1000),'strategy_id':'BOOSTER:test'}],[]);self.assertEqual(r['realized_total_usd'],-100);self.assertTrue(r['circuit_breaker_tripped'])
 def test_unknown_attribution(self):
  m=AdaptiveMLCalibrator(self.path);self.assertEqual(m._infer_strategy_for_symbol('BTCUSDT',0),m._infer_strategy_for_symbol('BTCUSDT',4));r=m.calibrate_from_closed_pnl([{'symbol':'BTCUSDT','closedPnl':'10','updatedTime':'1'}]);self.assertIsNone(r['portfolio_stats']['profit_factor']);self.assertEqual(r['strategy_list'][0]['weight_multiplier'],1.)
 def test_tp1_only_once(self):
  sp=SmartPositionManager('k','s');sp.guard_path=self.path/'guard.json'
  sp.ledger=NS(active_intent=lambda *a:{'intent_id':'confirmed','strategy_id':'BOOSTER:test','sl':99})
  pos={'symbol':'BTCUSDT','side':'Buy','positionIdx':0,'size':'10','avgPrice':'100','markPrice':'100','stopLoss':'99','takeProfit':'104'}
  sp.paged_get=lambda path,p:[pos] if path.endswith('/position/list') else []
  sp.place_tp1_half_limit=Mock(return_value={'orderId':'tp','ok':True});sp.signed_get=lambda *a:{'result':{'list':[{'orderStatus':'Filled','cumExecQty':'5'}]}}
  sp.audit_and_protect_open_positions();pos['size']='5';sp.audit_and_protect_open_positions();self.assertEqual(sp.place_tp1_half_limit.call_count,1)
 def test_split_toggle_honored(self):
  sp=SmartPositionManager('k','s');sp.guard_path=self.path/'guard.json';sp.split_tp_enabled=False
  pos={'symbol':'BTCUSDT','side':'Buy','size':'10','avgPrice':'100','markPrice':'100','stopLoss':'99','takeProfit':'104'}
  sp.paged_get=lambda path,p:[pos] if path.endswith('/position/list') else [];sp.place_tp1_half_limit=Mock();sp.audit_and_protect_open_positions();sp.place_tp1_half_limit.assert_not_called()
 def test_autopilot_survives_failure(self):
  s=ControlCenterState.__new__(ControlCenterState);s.scan_status={'heartbeat':0};s.autopilot_core_enabled=True;s.autopilot_booster_enabled=True;s.entry_paused=threading.Event();s._last_auto_ts=0;s.autopilot_interval_sec=0;s.booster_cycles_done=s.core_cycles_done=0;s.run_scan=Mock(side_effect=[ValueError('test'),{}]);s.save_persisted_state=Mock();s.add_log=Mock()
  class End(Exception):pass
  with patch('aitrade.dashboard.live_server.time.sleep',side_effect=[None,None,End]),self.assertRaises(End):s._autopilot_loop()
  self.assertEqual(s.run_scan.call_count,2);self.assertEqual(s.core_cycles_done,1)
 def test_csrf_and_invalid_content(self):
  state=NS(add_log=Mock());app=create_wsgi_app(state);codes=[]
  env={'REQUEST_METHOD':'POST','PATH_INFO':'/api/strategy_control','CONTENT_LENGTH':'2','wsgi.input':io.BytesIO(b'{}'),'CONTENT_TYPE':'application/json','HTTP_HOST':'example.com','HTTP_ORIGIN':'https://attacker.invalid','HTTP_X_REQUESTED_WITH':'AItrade'}
  result=app(env,lambda status,headers:codes.append(status));self.assertTrue(codes[0].startswith('400'));self.assertFalse(json.loads(result[0])['ok'])
 def test_ledger_survives_restart_and_deduplicates(self):
  a=Ledger(self.path/'test.db');a.put_records('closed',[{'orderId':'x','closedPnl':'1'}],'orderId');a.put_records('closed',[{'orderId':'x','closedPnl':'2'}],'orderId');b=Ledger(self.path/'test.db');self.assertEqual(len(b.records('closed')),1);self.assertEqual(b.records('closed')[0]['closedPnl'],'2')

 def core_scan(self,approved):
  s=ControlCenterState.__new__(ControlCenterState);s._lock=threading.RLock();s.entry_paused=threading.Event();s.settings={'universe':{'core_pairs':['BTCUSDT']}};s.ws_hub=NS(get_summary=lambda:{});s.regime_guard=NS(update_btc_regime=lambda _: {},can_open_position=lambda *a:(True,'ok'))
  s.x_radar=NS(refresh_feed=lambda:{},get_coin_modifier=lambda *a:{'allowed':True,'tag':'','score_bonus':0});s.oi_scanner=NS(scan_squeeze_setups=lambda *a:[])
  sig={'symbol':'BTCUSDT','strategy_id':'fixture','strategy_name':'fixture','side':'Buy','entry_price':100.,'stop_loss':99.,'take_profit_1':102.,'take_profit_2':103.,'position_qty':8.,'ml_win_probability':.8,'passed_ml_filter':approved,'debate_verdict':'APPROVED' if approved else 'REJECTED'}
  s.engine=NS(scan_market=lambda **kw:{'approved_signals':[sig] if approved else [],'rejected_signals':[] if approved else [sig]})
  s.bybit=NS(get_positions=lambda:[],get_closed_pnl=lambda:[],place_order=Mock(return_value={'ok':False,'retMsg':'mock'}))
  s.ml_calibrator=NS(state={'enabled':False},calibrate_from_closed_pnl=lambda _: {},get_strategy_modifier=lambda _: {'weight':1,'badge':'','prob_adj':0})
  s.booster=NS(compute_metrics=lambda *a:{});s.core_max_positions=3;s.core_min_prob_pct=60.;s.max_leverage=3;s.split_tp_enabled=True;s.add_log=Mock()
  s._run_scan(trade_core=True);return s
 def test_core_contract(self):
  s=self.core_scan(True);args=s.bybit.place_order.call_args.args;self.assertEqual(args[1],'Buy');self.assertEqual(args[3],8.);self.assertEqual(args[5],103.)
 def test_rejected_signal_never_executed(self):
  self.core_scan(False).bybit.place_order.assert_not_called()


 def test_post_submission_exception_blocks_retry(self):
  c=self.controller();c.spm.signed_post.side_effect=[{'retCode':0},KeyError('bad response')]
  self.assertFalse(self.enter(c)['ok']);self.assertEqual(len(c.ledger.unresolved()),1)
  self.assertFalse(self.enter(c)['ok']);self.assertEqual(c.spm.signed_post.call_count,2)
 def test_infinite_price_rejected(self):
  c=self.controller();self.assertFalse(self.enter(c,price=float('inf'))['ok']);c.spm.signed_post.assert_not_called()
 def test_tp1_small_position_never_closes_full(self):
  sp=SmartPositionManager('k','s');sp.get_spec=lambda s:{'min_qty':1.,'qty_step':1.,'tick_size':.1};sp.signed_post=Mock()
  r=sp.place_tp1_half_limit('BTCUSDT','Buy',1.,103.);self.assertTrue(r['skipped']);sp.signed_post.assert_not_called()
 def test_no_symbol_only_attribution(self):
  a=Ledger(self.path/'attribution.db');a.intent('x','BTCUSDT','BOOSTER:test',5,{});a.finish('x','FILLED',{})
  r=a.attribute([{'symbol':'BTCUSDT','createdTime':str(int(time.time()*1000)+1000)}]);self.assertEqual(r[0]['strategy_id'],'UNATTRIBUTED')


 def test_pending_not_repeated_and_then_reconciled(self):
  c=self.controller();self.assertTrue(self.enter(c)['ok']);self.assertEqual(len(c.ledger.unresolved()),1)
  self.assertFalse(self.enter(c)['ok'])
  c.spm.signed_get=lambda *a:{'result':{'list':[{'orderId':'o','orderStatus':'Filled','cumExecQty':'3.2'}]}}
  c.reconcile_pending();self.assertEqual(c.ledger.unresolved(),[])
  with c.ledger.connect() as db:
   payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0]);self.assertIn('sl',payload);self.assertEqual(payload['confirmedQty'],3.2)


 def test_portfolio_notional_cap(self):
  c=self.controller();c.get_positions=lambda:[{'symbol':'ETHUSDT','size':'40','avgPrice':'100','markPrice':'100','stopLoss':'99'}]
  self.assertFalse(self.enter(c)['ok']);c.spm.signed_post.assert_not_called()
 def test_narrow_stop_rejected(self):
  c=self.controller();self.assertFalse(self.enter(c,stop_loss=99.7,take_profit=102.0)['ok']);c.spm.signed_post.assert_not_called()
 def test_ledger_attribution_matches_order_and_side(self):
  a=Ledger(self.path/'attr2.db');a.intent('i1','BTCUSDT','BOOSTER:SQUEEZE',5,{'side':'Buy','orderId':'ord-1'});a.finish('i1','FILLED',{'orderId':'ord-1'})
  a.put_records('executions',[{'execId':'entry','orderId':'ord-1','execTime':'1','symbol':'BTCUSDT','side':'Buy','execQty':'10','closedSize':'0'}],'execId')
  self.assertEqual(a.latest_strategy('BTCUSDT','Buy'),'BOOSTER:SQUEEZE')
  self.assertEqual(a.latest_strategy('BTCUSDT','Sell'),'UNATTRIBUTED')
  a.put_records('executions',[{'execId':'exit','orderId':'close-1','execTime':'2','symbol':'BTCUSDT','side':'Sell','execQty':'10','closedSize':'10'}],'execId')
  r=a.attribute([{'symbol':'BTCUSDT','orderId':'close-1','closedSize':'10','side':'Sell','updatedTime':str(int(time.time()*1000)+1000)}])
  self.assertEqual(r[0]['strategy_id'],'BOOSTER:SQUEEZE')
 def test_breakeven_waits_for_one_r(self):
  sp=SmartPositionManager('k','s');sp.guard_path=self.path/'guard_r.json';sp.split_tp_enabled=False
  sp.ledger=NS(active_intent=lambda *a:{'intent_id':'confirmed','strategy_id':'BOOSTER:test','sl':98})
  pos={'symbol':'BTCUSDT','side':'Buy','positionIdx':0,'size':'10','avgPrice':'100','markPrice':'101.0','stopLoss':'98','takeProfit':'106'}
  sp.paged_get=lambda path,p:[pos] if path.endswith('/position/list') else []
  sp.move_sl_to_breakeven=Mock(return_value={'ok':True});sp.activate_trailing_stop=Mock(return_value={'ok':True})
  sp.audit_and_protect_open_positions();sp.move_sl_to_breakeven.assert_not_called()
  pos['markPrice']='102.1'
  sp.audit_and_protect_open_positions();sp.move_sl_to_breakeven.assert_called_once()

 def test_ws_rolling_windows_prune_stale(self):
  h=BybitV5WebSocketHub(['BTCUSDT']);now=time.time()
  h.trade_events['BTCUSDT'].append((now-1000.,5000.,True))
  h.trade_events['BTCUSDT'].append((now-120.,1000.,True))
  h.trade_events['BTCUSDT'].append((now-10.,400.,False))
  h.liq_events['BTCUSDT'].append((now-950.,9000.,True))
  h.liq_events['BTCUSDT'].append((now-40.,300.,False))
  m=h.get_symbol_metrics('BTCUSDT')
  self.assertEqual(m['cvd_1m_usd'],-400.);self.assertEqual(m['cvd_5m_usd'],600.);self.assertEqual(m['cvd_15m_usd'],600.)
  self.assertEqual(m['long_liq_15m_usd'],0.);self.assertEqual(m['short_liq_1m_usd'],300.)
 def test_reconcile_before_clear_unresolved(self):
  c=self.controller();c.ledger.intent('u1','BTCUSDT','BOOSTER:SQUEEZE',5,{'side':'Buy'})
  c.ledger.prepare_order({'orderLinkId':'u1','symbol':'BTCUSDT','side':'Buy','qty':'2.5'},'u1')
  c.ledger.finish('u1','UNCERTAIN',{'error':'timeout','requestedQty':2.5})
  c.spm.signed_get=Mock(return_value={'result':{'list':[{'orderId':'oid-1','orderStatus':'Filled','cumExecQty':'2.5'}]}})
  c.reconcile_and_clear_unresolved()
  self.assertEqual(c.ledger.unresolved(),[])
  self.assertEqual(c.ledger.latest_strategy('BTCUSDT','Buy'),'UNATTRIBUTED')
 def test_wide_spread_and_extreme_funding_rejected(self):
  c=self.controller();c.owner.ws_hub.get_bbo=lambda *a:(100.,100.12)
  self.assertFalse(self.enter(c)['ok']);c.spm.signed_post.assert_not_called()
  c.owner.ws_hub.get_bbo=lambda *a:(100.,100.01);c.owner.funding_rates={'BTCUSDT':0.0008}
  self.assertFalse(self.enter(c)['ok']);c.spm.signed_post.assert_not_called()
 def test_unrealized_drawdown_blocks_and_ledger_backup(self):
  c=self.controller();c.get_positions=lambda:[{'symbol':'ETHUSDT','size':'1','avgPrice':'100','markPrice':'90','stopLoss':'85','unrealisedPnl':'-11'}]
  self.assertFalse(self.enter(c)['ok']);c.spm.signed_post.assert_not_called()
  dest=c.ledger.backup_to(self.path/'backups',keep=2)
  self.assertTrue(dest.exists())

if __name__=='__main__':unittest.main()

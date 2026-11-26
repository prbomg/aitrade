"""Atomic state and durable execution journal; never stores API credentials."""
from contextlib import closing, contextmanager
import json, os, sqlite3, tempfile, threading, time
from pathlib import Path

_write_lock = threading.RLock()
def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        fd, name = tempfile.mkstemp(prefix='.'+path.name, dir=str(path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(value, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
            os.chmod(name, 0o600); os.replace(name, path)
        finally:
            if os.path.exists(name): os.unlink(name)

class Ledger:
    def __init__(self, path, environment='demo'):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True); self.environment=environment
        self._cache_lock = threading.RLock()
        self._cache_version = 0
        self._lifecycle_cache = None
        with self.connect() as c:
            try:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('PRAGMA synchronous=NORMAL')
            except Exception:
                pass
            c.executescript('''CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS exchange_records(environment TEXT, kind TEXT, id TEXT, payload TEXT, PRIMARY KEY(environment,kind,id));
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, environment TEXT, created REAL, symbol TEXT, strategy TEXT, risk REAL, status TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS orders(environment TEXT, link TEXT, intent_id TEXT, symbol TEXT, status TEXT, payload TEXT, PRIMARY KEY(environment,link));
            CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_intents_env_status ON intents(environment, status);
            CREATE INDEX IF NOT EXISTS idx_intents_env_sym_created ON intents(environment, symbol, created DESC);
            CREATE INDEX IF NOT EXISTS idx_orders_env_intent ON orders(environment, intent_id);''')
        os.chmod(self.path, 0o600)
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path, timeout=10)
        try:
            c.execute('PRAGMA busy_timeout=10000')
            with c:
                yield c
        finally:
            c.close()
    def event(self, kind, payload):
        with self.connect() as c:c.execute('INSERT INTO events(ts,kind,payload) VALUES(?,?,?)',(time.time(),kind,json.dumps(payload,ensure_ascii=False)))
    def _invalidate_cache(self):
        with self._cache_lock:
            self._cache_version += 1
            self._lifecycle_cache = None
    def put_records(self, kind, rows, key):
        with self.connect() as c:
            c.executemany('INSERT OR REPLACE INTO exchange_records VALUES(?,?,?,?)',[(self.environment,kind,str(r[key]),json.dumps(r,ensure_ascii=False)) for r in rows])
        self._invalidate_cache()
    def records(self, kind):
        with self.connect() as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM exchange_records WHERE environment=? AND kind=?',(self.environment,kind))]
    def intent(self, id, symbol, strategy, risk, payload):
        with self.connect() as c:c.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?)',(id,self.environment,time.time(),symbol,strategy,risk,'SUBMITTING',json.dumps(payload)))
        self._invalidate_cache()
    def finish(self, id, status, result):
        with self.connect() as c:
            row=c.execute('SELECT payload FROM intents WHERE id=?',(id,)).fetchone()
            payload=json.loads(row[0]) if row else {};payload.update(result)
            c.execute('UPDATE intents SET status=?,payload=? WHERE id=?',(status,json.dumps(payload),id))
        self._invalidate_cache()
    def unresolved(self, symbol=None):
        with self.connect() as c:
            if symbol:
                return c.execute("SELECT id FROM intents WHERE environment=? AND (status IN ('SUBMITTING','UNCERTAIN') OR (status='PENDING' AND symbol=?))",(self.environment,symbol)).fetchall()
            return c.execute("SELECT id FROM intents WHERE environment=? AND status IN ('SUBMITTING','UNCERTAIN','PENDING')",(self.environment,)).fetchall()
    def clear_unresolved(self):
        with self.connect() as c:c.execute("UPDATE intents SET status='RESET_CLEARED' WHERE environment=? AND status IN ('SUBMITTING','UNCERTAIN','PENDING')",(self.environment,))
        self._invalidate_cache()
    def today_count(self, since_ts=None):
        import datetime
        midnight=datetime.datetime.now(datetime.timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        cutoff=max(midnight, float(since_ts or 0))
        with self.connect() as c:return c.execute("SELECT count(*) FROM intents WHERE environment=? AND created>=? AND status!='REJECTED'",(self.environment,cutoff)).fetchone()[0]
    def prepare_order(self, payload, intent_id=None):
        """Commit identity BEFORE any network write; an absent ACK is never an absent order."""
        link=payload['orderLinkId']
        with self.connect() as c:
            if intent_id is None:
                row=c.execute('SELECT id FROM intents WHERE environment=? AND id=?',(self.environment,link)).fetchone()
                intent_id=row[0] if row else ''
            c.execute('INSERT OR IGNORE INTO orders VALUES(?,?,?,?,?,?)',
                      (self.environment,link,intent_id,payload['symbol'],'SUBMITTING',json.dumps(payload)))
        self._invalidate_cache()
    def order_result(self, link, status, result):
        with self.connect() as c:
            row=c.execute('SELECT payload FROM orders WHERE environment=? AND link=?',(self.environment,link)).fetchone()
            if row:
                p=json.loads(row[0]);p.update(result)
                c.execute('UPDATE orders SET status=?,payload=? WHERE environment=? AND link=?',
                          (status,json.dumps(p),self.environment,link))
        self._invalidate_cache()
    def orders_for(self, intent_id):
        with self.connect() as c:
            return [dict(json.loads(r[2]),journal_status=r[1],orderLinkId=r[0]) for r in c.execute(
                'SELECT link,status,payload FROM orders WHERE environment=? AND intent_id=?',(self.environment,intent_id))]
    def pending_exit_orders(self):
        terminal={'Filled','Cancelled','Rejected','PartiallyFilledCanceled','Deactivated','REJECTED'}
        with self.connect() as c:
            rows=c.execute('SELECT link,status,payload FROM orders WHERE environment=?',(self.environment,)).fetchall()
        out=[]
        for link,status,raw in rows:
            if status in terminal:
                continue
            parsed=json.loads(raw)
            if parsed.get('reduceOnly'):
                out.append(dict(parsed,orderLinkId=link,journal_status=status))
        return out

    def _execution_lifecycles(self):
        """Replay confirmed fills, including external fills, in exchange time order.
        Quantity conservation replaces symbol/time heuristics. Mixed ownership stays explicit.
        """
        with self._cache_lock:
            ver = self._cache_version
            if self._lifecycle_cache is not None and self._lifecycle_cache[0] == (self.environment, ver):
                return self._lifecycle_cache[1]
        with self.connect() as c:
            intents={r[0]:dict(json.loads(r[4]),strategy_id=r[2],symbol=r[3],created=r[1],intent_id=r[0]) for r in c.execute(
                'SELECT id,created,strategy,symbol,payload FROM intents WHERE environment=?',(self.environment,))}
            orders=list(c.execute('SELECT link,intent_id,payload FROM orders WHERE environment=?',(self.environment,)))
        links={i:i for i in intents};ids={}
        for i,p in intents.items():
            for oid in list(p.get('orderIds') or [])+[p.get('orderId')]:
                if oid:ids[str(oid)]=i
        for link,i,raw in orders:
            links[link]=i
            oid=json.loads(raw).get('orderId')
            if oid:ids[str(oid)]=i
        lots={};closes={};seen=set()
        for e in sorted(self.records('executions'),key=lambda e:(int(e.get('execTime') or 0),int(e.get('seq') or 0),str(e.get('execId') or ''))):
            eid=e.get('execId')
            if not eid or eid in seen or e.get('execType','Trade')!='Trade':continue
            seen.add(eid)
            qty=float(e.get('execQty') or 0);closed=min(qty,float(e.get('closedSize') or 0))
            sym=e.get('symbol');side=e.get('side');oid=str(e.get('orderId') or '')
            remaining=closed;bucket=lots.setdefault((sym,'Sell' if side=='Buy' else 'Buy'),[])
            allocations=closes.setdefault(oid,[]) if closed else []
            while remaining>1e-10 and bucket:
                lot=bucket[0];take=min(remaining,lot['remaining'])
                allocations.append((lot['intent_id'],take));lot['remaining']-=take;remaining-=take
                if lot['remaining']<1e-10:bucket.pop(0)
            if remaining>1e-10:allocations.append(('',remaining))
            opened=qty-closed
            if opened>1e-10:
                iid=links.get(e.get('orderLinkId')) or ids.get(oid,'')
                # Missing historical intent or order identity cannot inherit a strategy.
                iid=iid if iid in intents else ''
                bucket=lots.setdefault((sym,side),[])
                if bucket and any(p['intent_id']!=iid for p in bucket):
                    for p in bucket:p['intent_id']=''
                    iid=''
                bucket.append({'intent_id':iid,'remaining':opened,'opened_ms':int(e.get('execTime') or 0)})
        result = (intents, lots, closes)
        with self._cache_lock:
            if self._cache_version == ver:
                self._lifecycle_cache = ((self.environment, ver), result)
        return result
    def attribute(self, rows):
        intents,_,closes=self._execution_lifecycles();result=[]
        for row in rows:
            r=dict(row);alloc=closes.get(str(r.get('orderId') or ''),[])
            qty=float(r.get('closedSize') or r.get('qty') or 0)
            owners={i for i,q in alloc if q>1e-10}
            complete=qty>0 and abs(sum(q for _,q in alloc)-qty)<=max(1e-8,qty*1e-7)
            iid=next(iter(owners)) if complete and len(owners)==1 and '' not in owners else ''
            r.update(strategy_id=intents[iid]['strategy_id'] if iid else 'UNATTRIBUTED',entry_intent_id=iid,
                     entry_created_ms=int(intents[iid]['created']*1000) if iid else 0,
                     campaign_id=intents[iid].get('campaign_id') if iid else None,
                     attribution_status='Confirmed fill quantity lifecycle' if iid else 'Unconfirmed or mixed execution lifecycle',
                     attribution_allocations=[{'intent_id':i,'qty':q} for i,q in alloc])
            result.append(r)
        return result
    def active_intent(self, symbol, side, size=None):
        intents,lots,_=self._execution_lifecycles();active=lots.get((symbol,side),[])
        owners={p['intent_id'] for p in active};qty=sum(p['remaining'] for p in active)
        if not active or len(owners)!=1 or '' in owners:return None
        if size is not None and abs(qty-float(size))>max(1e-8,float(size)*1e-7):return None
        iid=next(iter(owners))
        return dict(intents[iid],remaining_qty=qty,opened_ms=min(p['opened_ms'] for p in active))
    def latest_strategy(self, symbol, side=None):
        if side is None:return 'UNATTRIBUTED'
        p=self.active_intent(symbol,side)
        return p['strategy_id'] if p else 'UNATTRIBUTED'

    def pending_strategy(self, symbol, side=None):
        with self.connect() as c:
            rows = c.execute(
                "SELECT strategy, payload FROM intents "
                "WHERE environment=? AND symbol=? AND status='PENDING' "
                "ORDER BY created DESC LIMIT 5",
                (self.environment, symbol),
            ).fetchall()
        for strat, p_json in rows:
            try:
                p = json.loads(p_json) if p_json else {}
            except Exception:
                p = {}
            if side and p.get('side') and str(p.get('side')) != str(side):
                continue
            return str(strat or 'UNATTRIBUTED')
        return 'UNATTRIBUTED'

    def clear_intent_if_absent(self, intent_id):
        with self.connect() as c:
            c.execute(
                "UPDATE intents SET status='RESET_CLEARED' WHERE environment=? AND id=? AND status IN ('SUBMITTING','UNCERTAIN')",
                (self.environment, intent_id),
            )
        self._invalidate_cache()

    def pending(self):
        with self.connect() as c:return [(r[0],r[1],json.loads(r[2])) for r in c.execute("SELECT id,symbol,payload FROM intents WHERE environment=? AND status='PENDING'",(self.environment,))]

    def prune_old_events(self, max_log_age_days: int = 14, max_decision_age_days: int = 30) -> int:
        """Удаляет устаревшие LOG и SIGNAL_DECISION события, сохраняя EXIT_SUBMITTED и свежую историю."""
        now = time.time()
        log_cutoff = now - max(1, int(max_log_age_days)) * 86400.0
        dec_cutoff = now - max(1, int(max_decision_age_days)) * 86400.0
        with self.connect() as c:
            cur1 = c.execute("DELETE FROM events WHERE kind='LOG' AND ts < ?", (log_cutoff,))
            cur2 = c.execute("DELETE FROM events WHERE kind='SIGNAL_DECISION' AND ts < ?", (dec_cutoff,))
            return int((cur1.rowcount or 0) + (cur2.rowcount or 0))

    def backup_to(self, backup_dir, keep=7):
        import datetime
        try:
            self.prune_old_events()
        except Exception:
            pass
        bdir = Path(backup_dir); bdir.mkdir(parents=True, exist_ok=True)
        day = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        dest = bdir / f"{self.path.stem}-{day}{self.path.suffix}"
        with self.connect() as src, closing(sqlite3.connect(dest)) as dst:
            with dst:
                src.backup(dst)
        files = sorted(bdir.glob(f"{self.path.stem}-*{self.path.suffix}"))
        for old in files[:-keep] if len(files) > keep else []:
            old.unlink(missing_ok=True)
        return dest

# -*- coding: utf-8 -*-
from __future__ import annotations
import base64, json, os, socket, ssl, struct, threading, time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple


class BybitV5WebSocketHub:
    WINDOW_MAX_SEC = 900.0  # 15m rolling window

    def __init__(self, symbols: List[str]) -> None:
        self.symbols = symbols[:40]
        self.host = "stream.bybit.com"
        self.port = 443
        self.path = "/v5/public/linear"
        self.connected = False
        self.msg_count = 0
        self.last_msg_ts = 0.0
        self._last_rx_ts = 0.0
        self.zombie_timeout_sec = 45.0
        self._running = False
        self._lock = threading.RLock()
        self.book_timestamps = {}
        self.stream_started_at = time.time()
        self.event_ids = {}

        self.orderbooks: Dict[str, Dict[str, Dict[float, float]]] = {}
        self.wall_first_seen: Dict[str, Dict[float, float]] = {}
        self.trade_events: Dict[str, Deque[Tuple[float, float, bool]]] = {}
        self.liq_events: Dict[str, Deque[Tuple[float, float, bool]]] = {}
        self.liquidations: List[Dict[str, Any]] = []

        for s in self.symbols:
            self.orderbooks[s] = {"bids": {}, "asks": {}}
            self.wall_first_seen[s] = {}
            self.trade_events[s] = deque()
            self.liq_events[s] = deque()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _create_ssl_socket(self) -> ssl.SSLSocket:
        raw = socket.create_connection((self.host, self.port), timeout=10)
        try:
            return ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise

    def get_bbo(self, symbol, max_age=5):
        with self._lock:
            book=self.orderbooks.get(symbol,{})
            if not self.connected or time.time()-self.book_timestamps.get(symbol,0)>max_age:
                raise RuntimeError("Устаревший стакан " + symbol)
            if not book.get("bids") or not book.get("asks"):raise RuntimeError("Пустой стакан " + symbol)
            bid=max(book["bids"]);ask=min(book["asks"])
            if bid>=ask:raise RuntimeError("Некорректный стакан " + symbol)
            return bid,ask

    @staticmethod
    def _send_ws_frame(sock: ssl.SSLSocket, opcode: int, payload: bytes) -> None:
        mask_key = os.urandom(4)
        header = bytearray([0x80 | (opcode & 0x0F)])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytearray(payload[i] ^ mask_key[i % 4] for i in range(length))
        sock.sendall(header + masked)

    @classmethod
    def _send_ws_text(cls, sock: ssl.SSLSocket, text: str) -> None:
        cls._send_ws_frame(sock, 0x1, text.encode("utf-8"))

    @staticmethod
    def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            buf.extend(chunk)
        return bytes(buf)

    def _read_ws_frame(self, sock: ssl.SSLSocket) -> Optional[str]:
        b1, b2 = self._recv_exact(sock, 2)
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(sock, 8))[0]
        mask_key = self._recv_exact(sock, 4) if masked else b""
        data = self._recv_exact(sock, length)
        if masked:
            data = bytes(data[i] ^ mask_key[i % 4] for i in range(length))
        self._last_rx_ts = time.time()
        if opcode == 0x8:
            raise ConnectionError("Server sent close frame")
        if opcode == 0x9:
            try:
                self._send_ws_frame(sock, 0xA, data[:125])
            except Exception:
                raise ConnectionError("Failed to send pong frame")
            return None
        if opcode == 0x1:
            return data.decode("utf-8", errors="ignore")
        return None

    def _run_loop(self) -> None:
        while self._running:
            sock = None
            try:
                sock = self._create_ssl_socket()
                key = base64.b64encode(os.urandom(16)).decode("ascii")
                handshake = (
                    f"GET {self.path} HTTP/1.1\r\n"
                    f"Host: {self.host}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                )
                sock.sendall(handshake.encode("ascii"))
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = sock.recv(1024)
                    if not chunk:
                        raise ConnectionError("Handshake EOF")
                    resp += chunk
                if b"101" not in resp.split(b"\r\n")[0]:
                    raise ConnectionError("Handshake failed")

                self.book_timestamps.clear()
                self.stream_started_at=time.time()
                self.connected = True
                topics: List[str] = []
                for s in self.symbols:
                    topics.append(f"orderbook.50.{s}")
                    topics.append(f"publicTrade.{s}")
                    topics.append(f"allLiquidation.{s}")

                for i in range(0, len(topics), 10):
                    sub_msg = json.dumps({"op": "subscribe", "args": topics[i:i+10]})
                    self._send_ws_text(sock, sub_msg)

                last_ping = time.time()
                self._last_rx_ts = last_ping
                sock.settimeout(5.0)
                while self._running:
                    now = time.time()
                    if now - self._last_rx_ts > self.zombie_timeout_sec:
                        raise ConnectionError("WebSocket stale/zombie timeout")
                    if now - last_ping >= 18.0:
                        self._send_ws_text(sock, json.dumps({"op": "ping"}))
                        last_ping = now
                    try:
                        raw_msg = self._read_ws_frame(sock)
                        if raw_msg:
                            self._handle_message(raw_msg)
                    except socket.timeout:
                        continue
            except Exception:
                self.connected = False
                time.sleep(3.0)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

    @staticmethod
    def _prune_deque(q: Deque[Tuple[float, float, bool]], cutoff: float) -> None:
        if not q:
            return
        while q and q[0][0] < cutoff:
            q.popleft()
        if any(e[0] < cutoff for e in q):
            valid = [e for e in q if e[0] >= cutoff]
            q.clear()
            q.extend(valid)

    def _handle_message(self, raw_msg: str) -> None:
        with self._lock:
            return self._handle_message_locked(raw_msg)

    def _handle_message_locked(self, raw_msg: str) -> None:
        try:
            msg = json.loads(raw_msg)
        except Exception:
            return
        self._last_rx_ts = time.time()
        if msg.get("op") == "pong" or msg.get("ret_msg") == "pong":
            self.last_msg_ts = self._last_rx_ts
            return
        topic = msg.get("topic", "")
        if not topic:
            return
        now = time.time()
        cutoff = now - self.WINDOW_MAX_SEC
        self.msg_count += 1
        self.last_msg_ts = now

        if topic.startswith("orderbook.50."):
            sym = topic.split(".")[-1]
            self.book_timestamps[sym] = now
            msg_type = msg.get("type", "")
            data = msg.get("data", {})
            ob = self.orderbooks.get(sym)
            if not ob:
                return
            walls = self.wall_first_seen.setdefault(sym, {})
            if msg_type == "snapshot":
                ob["bids"] = {float(p): float(v) for p, v in data.get("b", []) if float(v) > 0}
                ob["asks"] = {float(p): float(v) for p, v in data.get("a", []) if float(v) > 0}
                walls.clear()
                for p in list(ob["bids"].keys())[:15] + list(ob["asks"].keys())[:15]:
                    walls[p] = now
            else:
                for p_s, v_s in data.get("b", []):
                    p, v = float(p_s), float(v_s)
                    if v <= 0:
                        ob["bids"].pop(p, None)
                        walls.pop(p, None)
                    else:
                        if p not in ob["bids"]:
                            walls[p] = now
                        ob["bids"][p] = v
                for p_s, v_s in data.get("a", []):
                    p, v = float(p_s), float(v_s)
                    if v <= 0:
                        ob["asks"].pop(p, None)
                        walls.pop(p, None)
                    else:
                        if p not in ob["asks"]:
                            walls[p] = now
                        ob["asks"][p] = v

        elif topic.startswith("publicTrade."):
            sym = topic.split(".")[-1]
            q = self.trade_events.setdefault(sym, deque())
            for tr in msg.get("data", []):
                ev_ts=float(tr.get('T') or 0)/1000.0
                if ev_ts<cutoff or ev_ts>now+2:continue
                tid=str(tr.get('i') or '')
                if not tid:continue
                if not self._accept_event(('trade',sym,tid),ev_ts,cutoff):continue
                px = float(tr.get("p", 0) or 0)
                sz = float(tr.get("v", 0) or 0)
                usd = px * sz
                is_buy = tr.get("S", "Buy") == "Buy"
                q.append((ev_ts, usd, is_buy))
            self._prune_deque(q, cutoff)

        elif topic.startswith("allLiquidation."):
            sym = topic.split(".")[-1]
            items = msg.get("data", [])
            if isinstance(items, dict):
                items = [items]
            q = self.liq_events.setdefault(sym, deque())
            for it in items:
                ev_ms = float(it.get("T") or msg.get("ts") or 0)
                ev_ts = (ev_ms / 1000.0) if ev_ms > 0 else now
                if ev_ts < cutoff or ev_ts > now+2:continue
                key=('liq',sym,str(ev_ms),str(it.get('S')),str(it.get('p')),str(it.get('v')))
                if not self._accept_event(key,ev_ts,cutoff):continue
                px = float(it.get("p", 0) or it.get("price", 0) or 0)
                sz = float(it.get("v", 0) or it.get("size", 0) or 0)
                side = it.get("S", it.get("side", "Buy"))
                usd = round(px * sz, 2)
                is_long_liq = side == "Buy"
                q.append((ev_ts, usd, is_long_liq))
                entry = {
                    "time": time.strftime("%H:%M:%S"),
                    "symbol": sym,
                    "side": "LONG LIQ" if is_long_liq else "SHORT LIQ",
                    "price": px,
                    "size": sz,
                    "usd": usd,
                }
                self.liquidations.insert(0, entry)
                self.liquidations = self.liquidations[:50]
            self._prune_deque(q, cutoff)

    def _accept_event(self,key,ts,cutoff):
        # Time-based deduplication covers the entire rolling window at any activity rate.
        if not hasattr(self,'_dedup_prune_at') or time.time()-self._dedup_prune_at>30:
            self.event_ids={k:v for k,v in self.event_ids.items() if v>=cutoff}
            self._dedup_prune_at=time.time()
        if key in self.event_ids:return False
        self.event_ids[key]=ts
        return True

    def get_symbol_metrics(self, symbol: str) -> Dict[str, Any]:
        ob = self.orderbooks.get(symbol, {"bids": {}, "asks": {}})
        bids = sorted(ob["bids"].items(), key=lambda x: x[0], reverse=True)[:10]
        asks = sorted(ob["asks"].items(), key=lambda x: x[0])[:10]
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        mid = (best_bid + best_ask) * 0.5 if (best_bid > 0 and best_ask > 0) else 0.0
        spread_bps = ((best_ask - best_bid) / mid * 10000.0) if mid > 0 else 0.0

        bid_usd = sum(p * v for p, v in bids[:5])
        ask_usd = sum(p * v for p, v in asks[:5])
        obi_pct = ((bid_usd - ask_usd) / (bid_usd + ask_usd) * 100.0) if (bid_usd + ask_usd) > 0 else 0.0

        vamp = mid
        if bids and asks and (bids[0][1] + asks[0][1]) > 0:
            vamp = (best_bid * asks[0][1] + best_ask * bids[0][1]) / (bids[0][1] + asks[0][1])

        now = time.time()
        walls = self.wall_first_seen.get(symbol, {})
        max_wall_age = 0.0
        if bids or asks:
            top_levels = bids[:5] + asks[:5]
            largest_px = max(top_levels, key=lambda x: x[1])[0]
            first_t = walls.get(largest_px, now)
            max_wall_age = max(0.0, now - first_t)

        t_q = self.trade_events.setdefault(symbol, deque())
        self._prune_deque(t_q, now - self.WINDOW_MAX_SEC)
        buy_1m = sell_1m = buy_5m = sell_5m = buy_15m = sell_15m = 0.0
        for ts, usd, is_buy in t_q:
            age = now - ts
            if age <= 900.0:
                if is_buy:
                    buy_15m += usd
                else:
                    sell_15m += usd
            if age <= 300.0:
                if is_buy:
                    buy_5m += usd
                else:
                    sell_5m += usd
            if age <= 60.0:
                if is_buy:
                    buy_1m += usd
                else:
                    sell_1m += usd

        cvd_1m = buy_1m - sell_1m
        cvd_5m = buy_5m - sell_5m
        cvd_15m = buy_15m - sell_15m
        tot_5m = buy_5m + sell_5m
        flow_imb = (cvd_5m / tot_5m * 100.0) if tot_5m > 0 else 0.0

        l_q = self.liq_events.setdefault(symbol, deque())
        self._prune_deque(l_q, now - self.WINDOW_MAX_SEC)
        ll_1m = sl_1m = ll_5m = sl_5m = ll_15m = sl_15m = 0.0
        for ts, usd, is_long_liq in l_q:
            age = now - ts
            if age <= 900.0:
                if is_long_liq:
                    ll_15m += usd
                else:
                    sl_15m += usd
            if age <= 300.0:
                if is_long_liq:
                    ll_5m += usd
                else:
                    sl_5m += usd
            if age <= 60.0:
                if is_long_liq:
                    ll_1m += usd
                else:
                    sl_1m += usd

        return {
            "symbol": symbol,
            "flow_window_complete": self.connected and now-self.stream_started_at>=self.WINDOW_MAX_SEC,
            "flow_warmup_sec": max(0,round(self.WINDOW_MAX_SEC-(now-self.stream_started_at))),
            "mid_price": round(mid, 4),
            "vamp_price": round(vamp, 4),
            "spread_bps": round(spread_bps, 2),
            "obi_top5_pct": round(obi_pct, 1),
            "cvd_usd": round(cvd_5m, 0),
            "cvd_1m_usd": round(cvd_1m, 0),
            "cvd_5m_usd": round(cvd_5m, 0),
            "cvd_15m_usd": round(cvd_15m, 0),
            "flow_imbalance_pct": round(flow_imb, 1),
            "trades_count": len(t_q),
            "largest_wall_age_sec": round(max_wall_age, 1),
            "long_liq_usd": round(ll_15m, 0),
            "short_liq_usd": round(sl_15m, 0),
            "long_liq_1m_usd": round(ll_1m, 0),
            "short_liq_1m_usd": round(sl_1m, 0),
            "long_liq_5m_usd": round(ll_5m, 0),
            "short_liq_5m_usd": round(sl_5m, 0),
            "long_liq_15m_usd": round(ll_15m, 0),
            "short_liq_15m_usd": round(sl_15m, 0),
        }

    def get_summary(self) -> Dict[str, Any]:
        with self._lock:
            return self._get_summary_locked()

    def _get_summary_locked(self) -> Dict[str, Any]:
        rows = [self.get_symbol_metrics(s) for s in self.symbols]
        rows.sort(key=lambda r: abs(r["obi_top5_pct"]) + abs(r["flow_imbalance_pct"]), reverse=True)
        return {
            "connected": self.connected,
            "symbols_total":len(self.symbols),
            "flow_window_complete":self.connected and time.time()-self.stream_started_at>=self.WINDOW_MAX_SEC,
            "msg_count": self.msg_count,
            "last_msg_age_sec": round(time.time() - self.last_msg_ts, 1) if self.last_msg_ts > 0 else -1,
            "symbols_live": sum(1 for r in rows if r["mid_price"] > 0),
            "metrics": rows,
            "recent_liquidations": self.liquidations[:25],
        }

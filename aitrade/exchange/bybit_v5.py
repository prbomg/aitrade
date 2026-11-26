"""
Полнофункциональный коннектор к Bybit API V5 (https://api.bybit.com, https://api.bytick.com и https://api-demo.bybit.com).
Поддерживает:
- Автоматический обход сетевых/региональных блокировок и SSL-ошибок macOS: перебирает официальные домены Bybit V5
  (https://api-demo.bybit.com, https://api.bybit.com, https://api.bytick.com) с использованием системного SSL и резервного curl.
- Публичные данные: свечи OHLCV (/v5/market/kline), стакан L2 (/v5/market/orderbook),
  тикеры 24ч (/v5/market/tickers), открытый интерес (/v5/market/open-interest),
  история ставок финансирования (/v5/market/funding/history),
  лента последних сделок для расчета CVD и VPIN (/v5/market/recent-trade).
- Приватное исполнение на Bybit Unified Demo Trading (https://api-demo.bybit.com) с HMAC-SHA256 подписью:
  проверка демо-баланса (/v5/account/wallet-balance), установка плеча (/v5/position/set-leverage),
  выставление лимитных (Maker PostOnly) и рыночных ордеров с TP/SL (/v5/order/create),
  получение открытых позиций (/v5/position/list).
- Явное сохранение диагностической причины (self.last_network_error), если сеть недоступна.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import random
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Candle:
    timestamp: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass
class OrderBookLevel:
    price: float
    size: float

    @property
    def notional_usd(self) -> float:
        return self.price * self.size


@dataclass
class OrderBookSnapshot:
    symbol: str
    timestamp: int
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return 0.5 * (self.best_bid + self.best_ask)
        return max(self.best_bid, self.best_ask)

    @property
    def spread_pct(self) -> float:
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        return ((self.best_ask - self.best_bid) / mid) * 100.0


class BybitV5Client:
    """Клиент Bybit V5 с мультидоменным роутингом (api-demo.bybit.com / api.bybit.com / api.bytick.com)."""

    def __init__(self, settings: Dict[str, Any], allow_offline_fallback: bool = True) -> None:
        ex_cfg = settings.get("exchange", {})
        self.mode = ex_cfg.get("mode", "demo")
        self.category = ex_cfg.get("category", "linear")
        self.mainnet_url = ex_cfg.get("mainnet_url", "https://api.bybit.com").rstrip("/")
        self.demo_url = ex_cfg.get("demo_url", "https://api-demo.bybit.com").rstrip("/")
        self.recv_window = str(ex_cfg.get("recv_window", 5000))
        self.allow_offline_fallback = allow_offline_fallback

        # Официальные зеркала Bybit V5 (api-demo и api.bytick часто работают без VPN там, где блокируется api.bybit.com)
        self.public_endpoints: List[str] = [
            self.demo_url,
            self.mainnet_url,
            "https://api.bytick.com",
        ]
        self._active_public_url: str = self.demo_url

        if self.mode == "live":
            self.api_key = ex_cfg.get("live_api_key", "")
            self.api_secret = ex_cfg.get("live_api_secret", "")
            self.trade_base_url = self.mainnet_url
        else:
            self.api_key = ex_cfg.get("demo_api_key", "")
            self.api_secret = ex_cfg.get("demo_api_secret", "")
            self.trade_base_url = self.demo_url

        self._lock = threading.RLock()
        self._orderbook_history: Dict[str, List[OrderBookSnapshot]] = {}
        self._inst_cache: Dict[str, Tuple[float, float, float]] = {}
        self.last_data_source: str = "bybit_v5_live"
        self.last_network_error: str = ""
        self._ssl_ctx = ssl.create_default_context()

    # -------------------------------------------------------------------------
    # Низкоуровневые HTTP и HMAC-SHA256 методы Bybit V5
    # -------------------------------------------------------------------------
    def _sign_v5(self, timestamp_ms: str, payload_str: str) -> str:
        param_str = f"{timestamp_ms}{self.api_key}{self.recv_window}{payload_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _http_fetch_json(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        body_bytes: Optional[bytes] = None,
        timeout: float = 4.5,
    ) -> Dict[str, Any]:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AItrade/1.0",
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        try:
            req = urllib.request.Request(
                url, data=body_bytes, headers=req_headers, method=method.upper()
            )
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as exc:
            raise RuntimeError("Bybit transport: " + type(exc).__name__) from None

    def _public_get(self, path: str, params: Dict[str, Any], timeout: float = 4.5) -> Dict[str, Any]:
        query_str = urllib.parse.urlencode(params)
        with self._lock:
            active_url = self._active_public_url
            ordered_urls = [active_url] + [
                u for u in self.public_endpoints if u != active_url
            ]
        errors: List[str] = []

        for base_url in ordered_urls:
            url = f"{base_url}{path}?{query_str}"
            try:
                data = self._http_fetch_json(url, method="GET", timeout=timeout)
                if data.get("retCode", -1) != 0:
                    raise RuntimeError(
                        f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}"
                    )
                host_tag = base_url.replace("https://", "")
                with self._lock:
                    self._active_public_url = base_url
                    self.last_data_source = f"bybit_v5_live ({host_tag})"
                    self.last_network_error = ""
                return data.get("result", {})
            except Exception as exc:
                errors.append(f"{base_url.replace('https://', '')}: {exc}")

        err_msg = " | ".join(errors)
        with self._lock:
            self.last_network_error = err_msg
        raise RuntimeError(err_msg)

    def _private_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 7.0,
    ) -> Dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise PermissionError(
                "Ключи Bybit Demo API (BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET) не заданы в .env."
            )
        params = params or {}
        timestamp_ms = str(int(time.time() * 1000))
        if method.upper() == "GET":
            payload_str = urllib.parse.urlencode(sorted(params.items()))
            url = f"{self.trade_base_url}{path}" + (f"?{payload_str}" if payload_str else "")
            body_bytes = None
        else:
            payload_str = json.dumps(params, separators=(",", ":"))
            url = f"{self.trade_base_url}{path}"
            body_bytes = payload_str.encode("utf-8")

        signature = self._sign_v5(timestamp_ms, payload_str)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp_ms,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json",
        }
        data = self._http_fetch_json(
            url, method=method.upper(), headers=headers, body_bytes=body_bytes, timeout=timeout
        )
        if data.get("retCode", -1) != 0:
            raise RuntimeError(
                f"Bybit Demo API [{data.get('retCode')}]: {data.get('retMsg')}"
            )
        return data.get("result", {})

    # -------------------------------------------------------------------------
    # Публичные рыночные эндпоинты Bybit V5
    # -------------------------------------------------------------------------
    def get_tickers(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        try:
            res = self._public_get("/v5/market/tickers", params)
            return res.get("list", [])
        except Exception:
            if not self.allow_offline_fallback:
                raise
            self.last_data_source = "deterministic_market_replay"
            return self._synthetic_tickers(symbol)

    def get_klines(
        self,
        symbol: str,
        interval: str = "15",
        limit: int = 200,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[Candle]:
        params: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "interval": str(interval),
            "limit": min(limit, 1000),
        }
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)
        try:
            res = self._public_get("/v5/market/kline", params)
            raw_list = res.get("list", [])
            candles = [
                Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    turnover=float(row[6]),
                )
                for row in reversed(raw_list)
            ]
            return candles
        except Exception:
            if not self.allow_offline_fallback:
                raise
            self.last_data_source = "deterministic_market_replay"
            return self._synthetic_klines(symbol, interval, limit)

    def get_klines_paginated(
        self,
        symbol: str,
        interval: str = "15",
        days: int = 180,
        end_ms: Optional[int] = None,
        cache_dir: Optional[Any] = None,
        max_pages: int = 40,
    ) -> List[Candle]:
        """
        Загружает многомесячную историю свечей Bybit V5 с пагинацией по `end`
        и опциональным локальным кэшированием в `cache_dir` (data/candles_cache/).
        """
        from pathlib import Path
        from dataclasses import asdict
        from aitrade.storage import atomic_json

        iv_map = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "120": 120, "240": 240, "D": 1440}
        step_min = iv_map.get(str(interval), int(interval) if str(interval).isdigit() else 15)
        step_ms = step_min * 60 * 1000
        target_bars = max(200, int((days * 1440) / step_min))
        now_ms = int(end_ms if end_ms is not None else time.time() * 1000)
        start_cutoff = now_ms - target_bars * step_ms

        cache_file: Optional[Path] = None
        by_ts: Dict[int, Candle] = {}
        if cache_dir is not None:
            cdir = Path(cache_dir)
            cdir.mkdir(parents=True, exist_ok=True)
            cache_file = cdir / f"{symbol}_{interval}_{days}d.json"
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    for row in cached:
                        c = Candle(
                            timestamp=int(row["timestamp"]),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=float(row["volume"]),
                            turnover=float(row["turnover"]),
                        )
                        by_ts[c.timestamp] = c
                except Exception:
                    by_ts.clear()

        if len(by_ts) >= int(target_bars * 0.95):
            ordered = [by_ts[k] for k in sorted(by_ts.keys())]
            if now_ms - ordered[-1].timestamp <= step_ms * 4:
                return ordered[-target_bars:]

        cursor_end = now_ms
        pages = 0
        while pages < max_pages and len(by_ts) < target_bars:
            batch = self.get_klines(symbol, interval=str(interval), limit=1000, end=cursor_end)
            if not batch:
                break
            prev_len = len(by_ts)
            for c in batch:
                by_ts[c.timestamp] = c
            oldest_ts = min(c.timestamp for c in batch)
            pages += 1
            if oldest_ts <= start_cutoff or len(by_ts) == prev_len or len(batch) < 1000:
                break
            cursor_end = oldest_ts - step_ms

        ordered = [by_ts[k] for k in sorted(by_ts.keys()) if k >= start_cutoff - step_ms * 2]
        if len(ordered) > target_bars:
            ordered = ordered[-target_bars:]
        if cache_file is not None and ordered:
            atomic_json(cache_file, [asdict(c) for c in ordered])
        return ordered

    def get_orderbook(self, symbol: str, limit: int = 50) -> OrderBookSnapshot:
        params = {
            "category": self.category,
            "symbol": symbol,
            "limit": min(limit, 200),
        }
        try:
            res = self._public_get("/v5/market/orderbook", params)
            bids = [OrderBookLevel(float(p), float(q)) for p, q in res.get("b", [])]
            asks = [OrderBookLevel(float(p), float(q)) for p, q in res.get("a", [])]
            snap = OrderBookSnapshot(
                symbol=symbol,
                timestamp=int(res.get("ts", int(time.time() * 1000))),
                bids=bids,
                asks=asks,
            )
        except Exception:
            if not self.allow_offline_fallback:
                raise
            self.last_data_source = "deterministic_market_replay"
            snap = self._synthetic_orderbook(symbol, limit)

        with self._lock:
            hist = self._orderbook_history.setdefault(symbol, [])
            hist.append(snap)
            if len(hist) > 40:
                hist.pop(0)
        return snap

    def get_orderbook_history(self, symbol: str) -> List[OrderBookSnapshot]:
        with self._lock:
            return list(self._orderbook_history.get(symbol, []))

    def get_open_interest_history(
        self, symbol: str, interval_time: str = "15min", limit: int = 50
    ) -> List[Tuple[int, float]]:
        params = {
            "category": self.category,
            "symbol": symbol,
            "intervalTime": interval_time,
            "limit": min(limit, 200),
        }
        try:
            res = self._public_get("/v5/market/open-interest", params)
            rows = res.get("list", [])
            return [(int(r["timestamp"]), float(r["openInterest"])) for r in reversed(rows)]
        except Exception:
            if not self.allow_offline_fallback:
                raise
            return self._synthetic_open_interest(symbol, limit)

    def get_recent_trades(self, symbol: str, limit: int = 200) -> List[Dict[str, Any]]:
        params = {
            "category": self.category,
            "symbol": symbol,
            "limit": min(limit, 1000),
        }
        try:
            res = self._public_get("/v5/market/recent-trade", params)
            return res.get("list", [])
        except Exception:
            if not self.allow_offline_fallback:
                raise
            return self._synthetic_recent_trades(symbol, limit)

    def get_funding_history(self, symbol: str, limit: int = 12) -> List[float]:
        """Возвращает хронологический список последних 8h ставок финансирования (/v5/market/funding/history)."""
        params = {
            "category": self.category,
            "symbol": symbol,
            "limit": min(limit, 200),
        }
        try:
            res = self._public_get("/v5/market/funding/history", params)
            rows = res.get("list", [])
            return [float(r.get("fundingRate", 0.0) or 0.0) for r in reversed(rows)]
        except Exception:
            if not self.allow_offline_fallback:
                return []
            return [0.0001] * min(limit, 9)

    def get_funding_history_paginated(
        self,
        symbol: str,
        days: int = 180,
        end_ms: Optional[int] = None,
        cache_dir: Optional[Any] = None,
        max_pages: int = 12,
    ) -> List[Tuple[int, float]]:
        """
        Загружает многомесячную историю ставок финансирования Bybit V5 (/v5/market/funding/history)
        с пагинацией по `endTime` и локальным кэшем `data/candles_cache/{symbol}_funding_{days}d.json`.
        Возвращает хронологический список `(fundingRateTimestamp_ms, fundingRate)`.
        """
        from pathlib import Path
        from aitrade.storage import atomic_json

        now_ms = int(end_ms if end_ms is not None else time.time() * 1000)
        start_cutoff = now_ms - int(days * 86_400_000)
        eight_h_ms = 8 * 3_600_000

        cache_file: Optional[Path] = None
        by_ts: Dict[int, float] = {}
        if cache_dir is not None:
            cdir = Path(cache_dir)
            cdir.mkdir(parents=True, exist_ok=True)
            cache_file = cdir / f"{symbol}_funding_{days}d.json"
            if cache_file.exists():
                try:
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    for row in cached:
                        if isinstance(row, (list, tuple)) and len(row) >= 2:
                            by_ts[int(row[0])] = float(row[1])
                except Exception:
                    by_ts.clear()

        if by_ts:
            min_cached = min(by_ts.keys())
            max_cached = max(by_ts.keys())
            if min_cached <= start_cutoff + 86_400_000 and (now_ms - max_cached) <= 2 * eight_h_ms:
                return [(ts, by_ts[ts]) for ts in sorted(by_ts.keys()) if ts >= start_cutoff - 2 * eight_h_ms]

        cursor_end = now_ms
        pages = 0
        while pages < max_pages:
            params: Dict[str, Any] = {
                "category": self.category,
                "symbol": symbol,
                "limit": 200,
                "endTime": int(cursor_end),
            }
            try:
                res = self._public_get("/v5/market/funding/history", params)
                rows = res.get("list", [])
            except Exception:
                if not self.allow_offline_fallback:
                    raise
                break
            if not rows:
                break
            prev_len = len(by_ts)
            oldest_ts: Optional[int] = None
            for r in rows:
                ts_val = int(r.get("fundingRateTimestamp", 0) or 0)
                if ts_val <= 0:
                    continue
                fr_val = float(r.get("fundingRate", 0.0) or 0.0)
                by_ts[ts_val] = fr_val
                oldest_ts = ts_val if oldest_ts is None else min(oldest_ts, ts_val)
            pages += 1
            if oldest_ts is None or oldest_ts <= start_cutoff - eight_h_ms or len(by_ts) == prev_len or len(rows) < 200:
                break
            cursor_end = oldest_ts - 1

        ordered = [(ts, by_ts[ts]) for ts in sorted(by_ts.keys()) if ts >= start_cutoff - 2 * eight_h_ms]
        if cache_file is not None and ordered:
            atomic_json(cache_file, [[ts, rate] for ts, rate in ordered])
        return ordered

    # -------------------------------------------------------------------------
    # Приватные методы Bybit Unified Demo Trading (https://api-demo.bybit.com)
    # -------------------------------------------------------------------------
    def get_demo_wallet_balance(self, coin: str = "USDT") -> Dict[str, Any]:
        return self._private_request(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": coin},
        )

    def set_leverage(self, symbol: str, leverage: float) -> Dict[str, Any]:
        lev_str = str(int(max(1, min(leverage, 10))))
        try:
            return self._private_request(
                "POST",
                "/v5/position/set-leverage",
                {
                    "category": self.category,
                    "symbol": symbol,
                    "buyLeverage": lev_str,
                    "sellLeverage": lev_str,
                },
            )
        except RuntimeError as e:
            if "110043" in str(e):
                return {"status": "leverage_unchanged", "leverage": lev_str}
            raise

    def get_instrument_lot_and_tick(self, symbol: str) -> Tuple[float, float, float]:
        """
        Запрашивает точную спецификацию контракта через /v5/market/instruments-info
        (qtyStep, minOrderQty, tickSize) согласно стандарту Bybit OpenAPI V5.
        """
        with self._lock:
            if symbol in self._inst_cache:
                return self._inst_cache[symbol]
        try:
            res = self._public_get("/v5/market/instruments-info", {"category": self.category, "symbol": symbol})
            items = res.get("list", [])
            if items:
                info = items[0]
                lot_f = info.get("lotSizeFilter", {})
                px_f = info.get("priceFilter", {})
                qty_step = float(lot_f.get("qtyStep", 0.01) or 0.01)
                min_qty = float(lot_f.get("minOrderQty", qty_step) or qty_step)
                tick_size = float(px_f.get("tickSize", 0.0001) or 0.0001)
                with self._lock:
                    self._inst_cache[symbol] = (qty_step, min_qty, tick_size)
                return qty_step, min_qty, tick_size
        except Exception:
            pass
        defaults = {
            "BTCUSDT": (0.001, 0.001, 0.1),
            "ETHUSDT": (0.01, 0.01, 0.01),
            "SOLUSDT": (0.1, 0.1, 0.01),
            "BNBUSDT": (0.01, 0.01, 0.1),
            "XRPUSDT": (1.0, 1.0, 0.0001),
            "ADAUSDT": (1.0, 1.0, 0.0001),
            "DOGEUSDT": (1.0, 1.0, 0.00001),
            "SUIUSDT": (1.0, 1.0, 0.0001),
            "LINKUSDT": (0.1, 0.1, 0.001),
            "AVAXUSDT": (0.1, 0.1, 0.01),
            "NEARUSDT": (0.1, 0.1, 0.001),
            "TONUSDT": (0.1, 0.1, 0.001),
            "DOTUSDT": (0.1, 0.1, 0.001),
            "APTUSDT": (0.01, 0.01, 0.001),
            "ARBUSDT": (0.1, 0.1, 0.0001),
            "OPUSDT": (0.1, 0.1, 0.0001),
            "LTCUSDT": (0.1, 0.1, 0.01),
            "ATOMUSDT": (0.1, 0.1, 0.001),
            "INJUSDT": (0.1, 0.1, 0.001),
            "FETUSDT": (1.0, 1.0, 0.0001),
        }
        return defaults.get(symbol, (0.1, 0.1, 0.0001))

    @staticmethod
    def _format_step(value: float, step: float) -> str:
        from decimal import Decimal, ROUND_DOWN
        d_val = Decimal(str(value))
        d_step = Decimal(str(step))
        quantized = (d_val / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
        s = format(quantized, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    def place_demo_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Limit",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Отправляет ордер в Bybit Unified Demo Trading (POST /v5/order/create)
        с автоматическим выравниванием qty по qtyStep и цен по tickSize.
        """
        qty_step, min_qty, tick_size = self.get_instrument_lot_and_tick(symbol)
        safe_qty = max(qty, min_qty)
        qty_str = self._format_step(safe_qty, qty_step)

        time_in_force = "PostOnly" if (order_type == "Limit" and post_only) else "GTC"
        if order_type == "Market":
            time_in_force = "IOC"

        payload: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty_str,
            "timeInForce": time_in_force,
            "positionIdx": 0,
        }
        if order_type == "Limit" and price is not None:
            payload["price"] = self._format_step(price, tick_size)
        if stop_loss is not None:
            payload["stopLoss"] = self._format_step(stop_loss, tick_size)
            payload["slTriggerBy"] = "MarkPrice"
            payload["tpslMode"] = "Full"
        if take_profit is not None:
            payload["takeProfit"] = self._format_step(take_profit, tick_size)
            payload["tpTriggerBy"] = "LastPrice"
            payload["tpslMode"] = "Full"

        return self._private_request("POST", "/v5/order/create", payload)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Запрашивает активные неисполненные ордера на Bybit V5 (GET /v5/order/realtime)."""
        params: Dict[str, Any] = {"category": self.category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        res = self._private_request("GET", "/v5/order/realtime", params)
        return res.get("list", [])

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Запрашивает открытые позиции на Bybit V5 (GET /v5/position/list)."""
        params: Dict[str, Any] = {"category": self.category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        res = self._private_request("GET", "/v5/position/list", params)
        return res.get("list", [])

    # -------------------------------------------------------------------------
    # Детерминированная генерация реалистичных рыночных структур (Offline / Test Mode)
    # -------------------------------------------------------------------------
    _BASE_PRICES: Dict[str, float] = {
        "BTCUSDT": 68400.0,
        "ETHUSDT": 2650.0,
        "SOLUSDT": 178.5,
        "BNBUSDT": 595.0,
        "XRPUSDT": 0.548,
        "ADAUSDT": 0.365,
        "DOGEUSDT": 0.162,
        "AVAXUSDT": 28.4,
        "LINKUSDT": 14.8,
        "SUIUSDT": 2.15,
        "TONUSDT": 5.12,
        "DOTUSDT": 4.45,
        "NEARUSDT": 4.85,
        "APTUSDT": 9.60,
        "ARBUSDT": 0.58,
        "OPUSDT": 1.65,
        "LTCUSDT": 72.5,
        "ATOMUSDT": 4.75,
        "INJUSDT": 21.8,
        "FETUSDT": 1.38,
    }

    def _seed_for(self, key: str) -> random.Random:
        digest = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], 16)
        return random.Random(digest)

    def _synthetic_tickers(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        symbols = [symbol] if symbol else list(self._BASE_PRICES.keys())
        out: List[Dict[str, Any]] = []
        for idx, sym in enumerate(symbols):
            base = self._BASE_PRICES.get(sym, 10.0)
            spread_frac = 0.00018 + (idx % 5) * 0.00006
            bid = base * (1.0 - spread_frac / 2.0)
            ask = base * (1.0 + spread_frac / 2.0)
            funding = 0.00045 if idx % 3 == 0 else (-0.00038 if idx % 3 == 1 else 0.00010)
            turnover = 85_000_000.0 * (1.0 + (20 - idx) * 0.35)
            out.append(
                {
                    "symbol": sym,
                    "lastPrice": f"{base:.6f}",
                    "bid1Price": f"{bid:.6f}",
                    "ask1Price": f"{ask:.6f}",
                    "turnover24h": f"{turnover:.2f}",
                    "volume24h": f"{turnover / base:.2f}",
                    "fundingRate": f"{funding:.6f}",
                    "openInterestValue": f"{turnover * 0.42:.2f}",
                    "price24hPcnt": f"{((idx % 7) - 3) * 0.014:.4f}",
                }
            )
        return out

    def _synthetic_klines(
        self, symbol: str, interval: str = "15", limit: int = 200
    ) -> List[Candle]:
        rng = self._seed_for(f"{symbol}_{interval}")
        base_price = self._BASE_PRICES.get(symbol, 100.0)
        interval_min = int(interval) if str(interval).isdigit() else 60
        step_ms = interval_min * 60 * 1000
        anchor_ts = 1762785000000
        start_ts = anchor_ts - (limit * step_ms)

        candles: List[Candle] = []
        price = base_price * 0.992
        base_vol = 250000.0 / base_price
        archetype = sum(ord(c) for c in symbol) % 4

        for i in range(limit):
            ts = start_ts + i * step_ms
            wave = math.sin(i / 9.0) * 0.0032 + math.cos(i / 21.0) * 0.0018
            noise = rng.uniform(-0.0012, 0.0012)
            open_p = price
            close_p = base_price * (1.0 + wave + noise)
            high_p = max(open_p, close_p) * (1.0 + rng.uniform(0.0008, 0.0024))
            low_p = min(open_p, close_p) * (1.0 - rng.uniform(0.0008, 0.0024))
            vol_mult = rng.uniform(0.75, 1.35)

            if i == limit - 3:
                if archetype == 0:
                    low_p = base_price * 0.979
                    close_p = base_price * 0.986
                    vol_mult = 2.6
                elif archetype == 1:
                    high_p = base_price * 1.021
                    close_p = base_price * 1.015
                    vol_mult = 2.4
            elif i == limit - 2:
                if archetype == 0:
                    open_p = candles[-1].close
                    close_p = base_price * 0.996
                    high_p = base_price * 0.998
                    low_p = open_p * 0.999
                    vol_mult = 2.9
                elif archetype == 1:
                    open_p = candles[-1].close
                    high_p = base_price * 1.023
                    close_p = base_price * 1.007
                    low_p = base_price * 1.005
                    vol_mult = 2.7
                elif archetype == 2:
                    open_p = base_price * 0.999
                    close_p = base_price * 1.008
                    high_p = base_price * 1.009
                    low_p = base_price * 0.998
                    vol_mult = 3.1
            elif i == limit - 1:
                if archetype == 0:
                    open_p = candles[-1].close
                    low_p = base_price * 0.989
                    close_p = base_price * 0.993
                    high_p = base_price * 0.995
                    vol_mult = 2.2
                elif archetype == 1:
                    open_p = candles[-1].close
                    high_p = open_p * 1.001
                    close_p = base_price * 1.003
                    low_p = base_price * 1.001
                    vol_mult = 2.1
                elif archetype == 2:
                    open_p = candles[-1].close
                    close_p = base_price * 1.011
                    high_p = base_price * 1.012
                    low_p = open_p * 0.999
                    vol_mult = 2.4
                else:
                    open_p = base_price * 0.985
                    low_p = base_price * 0.977
                    close_p = base_price * 0.991
                    high_p = base_price * 0.992
                    vol_mult = 2.8

            vol = base_vol * vol_mult
            candles.append(
                Candle(
                    timestamp=ts,
                    open=round(open_p, 6),
                    high=round(high_p, 6),
                    low=round(low_p, 6),
                    close=round(close_p, 6),
                    volume=round(vol, 4),
                    turnover=round(vol * close_p, 2),
                )
            )
            price = close_p
        return candles

    def _synthetic_orderbook(self, symbol: str, limit: int = 50) -> OrderBookSnapshot:
        candles = self._synthetic_klines(symbol, "15", 10)
        last_close = candles[-1].close
        tick = last_close * 0.00015
        archetype = sum(ord(c) for c in symbol) % 4
        bid_bias = 1.45 if archetype in (0, 2, 3) else 0.72
        ask_bias = 0.75 if archetype in (0, 2, 3) else 1.48

        bids: List[OrderBookLevel] = []
        asks: List[OrderBookLevel] = []
        base_notional = 45_000.0
        for lvl in range(1, min(limit, 50) + 1):
            b_price = last_close - lvl * tick
            a_price = last_close + lvl * tick
            b_wall = 9.5 if (lvl == 3 and bid_bias > 1.0) else 1.0
            a_wall = 9.5 if (lvl == 3 and ask_bias > 1.0) else 1.0
            b_size = (base_notional * bid_bias * b_wall * (1.0 + 0.08 * lvl)) / b_price
            a_size = (base_notional * ask_bias * a_wall * (1.0 + 0.08 * lvl)) / a_price
            bids.append(OrderBookLevel(round(b_price, 6), round(b_size, 4)))
            asks.append(OrderBookLevel(round(a_price, 6), round(a_size, 4)))

        return OrderBookSnapshot(
            symbol=symbol,
            timestamp=int(time.time() * 1000),
            bids=bids,
            asks=asks,
        )

    def _synthetic_open_interest(self, symbol: str, limit: int = 50) -> List[Tuple[int, float]]:
        base_price = self._BASE_PRICES.get(symbol, 100.0)
        base_oi = 120_000_000.0 / base_price
        archetype = sum(ord(c) for c in symbol) % 4
        anchor_ts = 1762785000000
        step_ms = 15 * 60 * 1000
        out: List[Tuple[int, float]] = []
        for i in range(limit):
            ts = anchor_ts - (limit - i) * step_ms
            if i >= limit - 3 and archetype in (1, 3):
                oi = base_oi * (1.0 - 0.018 * (i - (limit - 4)))
            else:
                oi = base_oi * (1.0 + 0.002 * math.sin(i / 4.0) + 0.0008 * i)
            out.append((ts, round(oi, 2)))
        return out

    def _synthetic_recent_trades(self, symbol: str, limit: int = 200) -> List[Dict[str, Any]]:
        candles = self._synthetic_klines(symbol, "15", 5)
        last_p = candles[-1].close
        archetype = sum(ord(c) for c in symbol) % 4
        buy_dominant = archetype in (0, 2, 3)
        trades: List[Dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        for i in range(limit):
            is_buy = (i % 4 != 0) if buy_dominant else (i % 4 == 0)
            size = (12000.0 if (i % 9 == 0) else 2800.0) / last_p
            trades.append(
                {
                    "execId": f"sim_{symbol}_{i}",
                    "symbol": symbol,
                    "price": f"{last_p:.6f}",
                    "size": f"{size:.4f}",
                    "side": "Buy" if is_buy else "Sell",
                    "time": str(now_ms - (limit - i) * 400),
                }
            )
        return trades

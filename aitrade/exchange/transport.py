"""Verified TLS, no secrets in argv, no automatic POST replay."""
import hashlib, hmac, json, os, threading, time, urllib.parse, urllib.request


class ExchangeError(RuntimeError):
    def __init__(self, message, code=None, uncertain=False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


_time_lock = threading.Lock()
_time_offset_ms = 0
_RETRYABLE_GET_CODES = frozenset({10006, 10016})


def _sync_server_time_offset(base_url: str, timeout: float = 5.0) -> int:
    global _time_offset_ms
    try:
        with urllib.request.urlopen(base_url + "/v5/market/time", timeout=timeout) as r:
            data = json.load(r)
        res = data.get("result") or {}
        srv_ms = int(res.get("timeNano", 0)) // 1_000_000 if res.get("timeNano") else int(res.get("timeSecond", 0)) * 1000
        if srv_ms > 0:
            offset = srv_ms - int(time.time() * 1000)
            with _time_lock:
                _time_offset_ms = offset
            return offset
    except Exception:
        pass
    return _time_offset_ms


def request(base_url, key, secret, method, path, params, timeout=8):
    if method == "POST" and os.environ.get("AITRADE_READ_ONLY") == "1":
        raise ExchangeError("Read-only validation mode")
    if not key or not secret:
        raise ExchangeError("Биржевые ключи не настроены")
    if base_url not in ("https://api-demo.bybit.com", "https://api.bybit.com"):
        raise ExchangeError("Недопустимый адрес биржи")

    max_attempts = 3 if method == "GET" else 1
    synced_clock = False

    for attempt in range(max_attempts):
        with _time_lock:
            offset = _time_offset_ms
        ts = str(int(time.time() * 1000) + offset)
        recv = "10000"
        payload = (
            urllib.parse.urlencode(sorted(params.items()))
            if method == "GET"
            else json.dumps(params, separators=(",", ":"))
        )
        sign = hmac.new(secret.encode(), (ts + key + recv + payload).encode(), hashlib.sha256).hexdigest()
        url = base_url + path + ("?" + payload if method == "GET" and payload else "")
        headers = {
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sign,
        }
        try:
            req = urllib.request.Request(
                url,
                data=payload.encode() if method == "POST" else None,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
        except Exception as e:
            if method == "GET" and attempt + 1 < max_attempts:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise ExchangeError("Ошибка связи с Bybit: " + type(e).__name__, uncertain=(method == "POST")) from None

        if not isinstance(data, dict) or data.get("retCode") != 0:
            code = data.get("retCode") if isinstance(data, dict) else None
            if path.endswith("/set-leverage") and code == 110043:
                return dict(data, retCode=0)
            if code == 10002 and not synced_clock:
                synced_clock = True
                _sync_server_time_offset(base_url, timeout=min(5.0, float(timeout)))
                if method == "GET" and attempt + 1 < max_attempts:
                    continue
            if method == "GET" and code in _RETRYABLE_GET_CODES and attempt + 1 < max_attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise ExchangeError(
                "Bybit " + str(code) + ": " + str(data.get("retMsg", "Некорректный ответ") if isinstance(data, dict) else "Некорректный ответ")[:180],
                code=code,
            )
        return data

# -*- coding: utf-8 -*-
"""
Информационная панель OI / Liquidation Squeeze Scanner (отключена от автоторговли до валидации):
- Расчёт ATR и Anti-High/Anti-Low строго по ЗАКРЫТЫМ свечам (k_list[1:]).
- Исправлены шумовые пороги: ликвидации >= $50,000, OI delta >= 1.5%, funding >= 0.03%.
- Параллельный опрос символов через ThreadPoolExecutor (без блокировки глобального лока).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple


class OISqueezeScanner:
    MIN_LIQ_USD = 50_000.0
    MIN_OI_DELTA_PCT = 1.50
    MIN_FUNDING_PCT = 0.030
    ENDPOINTS = ("https://api.bybit.com", "https://api-demo.bybit.com", "https://api.bytick.com")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.last_scan_ts = "—"
        self.cached_signals: List[Dict[str, Any]] = []
        self._active_url = self.ENDPOINTS[0]

    def _curl_json(self, url: str) -> Dict[str, Any]:
        path_suffix = None
        for ep in self.ENDPOINTS:
            if url.startswith(ep):
                path_suffix = url[len(ep):]
                break
        if path_suffix is None and url.startswith("/"):
            path_suffix = url
        with self._lock:
            active_ep = self._active_url
        candidates = (
            [active_ep + path_suffix] + [ep + path_suffix for ep in self.ENDPOINTS if ep != active_ep]
            if path_suffix is not None
            else [url]
        )
        last_exc: Optional[Exception] = None
        for cand_url in candidates:
            try:
                with urllib.request.urlopen(cand_url, timeout=6) as r:
                    data = json.load(r)
                if not isinstance(data, dict) or data.get("retCode") != 0:
                    raise RuntimeError("Ошибка рыночного API Bybit")
                if path_suffix is not None:
                    with self._lock:
                        self._active_url = cand_url[:-len(path_suffix)]
                return data
            except Exception as exc:
                last_exc = exc
        raise RuntimeError("Нет подтверждённых данных рынка: " + type(last_exc).__name__) from None

    def _compute_15m_atr_and_anti_chase(self, sym: str, last_px: float) -> Tuple[float, bool, bool, str]:
        # Загружаем 17 свечей 15m и отбрасываем k_list[0] (текущая незакрытая свеча!)
        k_url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval=15&limit=17"
        k_resp = self._curl_json(k_url)
        k_list = k_resp.get("result", {}).get("list", [])
        closed = k_list[1:]  # Строго закрытые бары (k_list[0] — незакрытая свеча)
        if len(closed) < 6 or last_px <= 0:
            raise RuntimeError("Недостаточно закрытых свечей для ATR: " + sym)

        trs: List[float] = []
        for i in range(min(14, len(closed) - 1)):
            hi = float(closed[i][2] or last_px)
            lo = float(closed[i][3] or last_px)
            prev_close = float(closed[i + 1][4] or last_px)
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
            trs.append(tr / max(1e-9, last_px) * 100.0)

        atr_pct = sum(trs) / len(trs) if trs else 0.85
        atr_pct = max(0.45, min(2.50, atr_pct))

        open_30m = float(closed[1][1] or last_px)
        close_last = float(closed[0][4] or last_px)
        move_30m_pct = ((close_last - open_30m) / max(1e-9, open_30m)) * 100.0

        high_1h = max(float(closed[i][2] or last_px) for i in range(min(4, len(closed))))
        low_1h = min(float(closed[i][3] or last_px) for i in range(min(4, len(closed))))
        span_1h = max(1e-9, high_1h - low_1h)
        pos_in_1h = (close_last - low_1h) / span_1h

        overbought_long = (move_30m_pct > max(1.55, atr_pct * 1.85)) and (pos_in_1h > 0.88)
        oversold_short = (move_30m_pct < -max(1.55, atr_pct * 1.85)) and (pos_in_1h < 0.12)

        info_str = f"ATR15m {atr_pct:.2f}%, импульс 30м {move_30m_pct:+.2f}% (closed)"
        return round(atr_pct, 2), overbought_long, oversold_short, info_str

    def _scan_one(
        self, sym: str, tk: Dict[str, Any], wsm: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        last_px = float(tk.get("lastPrice", 0) or 0)
        if last_px <= 0:
            return None
        funding_rate_pct = float(tk.get("fundingRate", 0) or 0) * 100.0
        chg_24h_pct = float(tk.get("price24hPcnt", 0) or 0) * 100.0
        oi_val = float(tk.get("openInterestValue", 0) or 0)

        oi_url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={sym}&intervalTime=15min&limit=4"
        oi_resp = self._curl_json(oi_url)
        oi_items = oi_resp.get("result", {}).get("list", [])
        if len(oi_items) < 2:
            return None
        latest_oi = float(oi_items[0].get("openInterest", 0) or 0)
        prev_oi = float(oi_items[-1].get("openInterest", 0) or 0)
        oi_delta_pct = ((latest_oi - prev_oi) / prev_oi) * 100.0 if prev_oi > 0 else 0.0

        atr_15m_pct, is_overbought_high, is_oversold_low, atr_note = self._compute_15m_atr_and_anti_chase(sym, last_px)

        obi_pct = float(wsm.get("obi_top5_pct", 0.0) or 0.0)
        cvd_usd = float(wsm.get("cvd_usd", 0.0) or 0.0)
        long_liq = float(wsm.get("long_liq_5m_usd", wsm.get("long_liq_usd", 0.0)) or 0.0)
        short_liq = float(wsm.get("short_liq_5m_usd", wsm.get("short_liq_usd", 0.0)) or 0.0)

        setup_type = "MONITOR"
        direction = "LONG"
        grade = "B"
        score = 50.0
        rationale = "Нейтральный баланс OI и фандинга"

        if (oi_delta_pct >= self.MIN_OI_DELTA_PCT or short_liq >= self.MIN_LIQ_USD) and (
            funding_rate_pct <= -self.MIN_FUNDING_PCT or (obi_pct >= 18.0 and cvd_usd > 0)
        ):
            direction = "LONG"
            if is_overbought_high:
                setup_type = "WAIT PULLBACK (АНТИ-ХАЙ)"
                score = 62.0
                grade = "B"
                rationale = "🛡 Анти-Хай блок: " + atr_note
            else:
                setup_type = "SHORT SQUEEZE BREAKOUT"
                score = min(96.0, 68.0 + max(0.0, oi_delta_pct * 3.0) + max(0.0, obi_pct * 0.25))
                grade = "A+" if (score >= 76.0 and obi_pct >= 10.0 and cvd_usd > 0) else "A"
                rationale = f"OI {oi_delta_pct:+.2f}%, Funding {funding_rate_pct:+.4f}%, ShortLiq ${short_liq:.0f} | {atr_note}"
        elif long_liq >= self.MIN_LIQ_USD and obi_pct >= 18.0 and cvd_usd > 0:
            setup_type = "LIQ CASCADE REVERSAL"
            direction = "LONG"
            score = min(95.0, 74.0 + obi_pct * 0.30)
            grade = "A+"
            rationale = f"Каскад лонг-ликвидаций (${long_liq:.0f}) выкуплен плитой OBI {obi_pct:+.1f}% | {atr_note}"
        elif (funding_rate_pct >= self.MIN_FUNDING_PCT or oi_delta_pct >= self.MIN_OI_DELTA_PCT) and (
            obi_pct <= -18.0 and cvd_usd < 0
        ):
            direction = "SHORT"
            if is_oversold_low:
                setup_type = "WAIT BOUNCE (АНТИ-ЛОУ)"
                score = 61.0
                grade = "B"
                rationale = "🛡 Анти-Лоу блок: " + atr_note
            else:
                setup_type = "LONG SQUEEZE / DUMP"
                score = min(94.0, 67.0 + max(0.0, oi_delta_pct * 3.0) + abs(min(0.0, obi_pct * 0.25)))
                grade = "A+" if (score >= 76.0 and obi_pct <= -12.0) else "A"
                rationale = f"Давление продавцов (OBI {obi_pct:+.1f}%, Funding {funding_rate_pct:+.4f}%) | {atr_note}"
        else:
            direction = "LONG" if (obi_pct + chg_24h_pct) >= 0 else "SHORT"
            score = round(50.0 + min(18.0, abs(oi_delta_pct) * 2.5 + abs(obi_pct) * 0.2), 1)
            grade = "B"
            rationale = f"OI {oi_delta_pct:+.2f}%, OBI {obi_pct:+.1f}% | {atr_note}"

        sl_pct = max(1.20, min(2.60, atr_15m_pct * 1.80)) / 100.0
        tp1_pct = sl_pct * 1.80
        tp2_pct = sl_pct * 3.00

        pullback_frac = (0.15 * atr_15m_pct) / 100.0
        if direction == "LONG":
            entry_px = round(last_px * (1.0 - pullback_frac), 6)
            sl_px = round(entry_px * (1.0 - sl_pct), 6)
            tp1_px = round(entry_px * (1.0 + tp1_pct), 6)
            tp2_px = round(entry_px * (1.0 + tp2_pct), 6)
        else:
            entry_px = round(last_px * (1.0 + pullback_frac), 6)
            sl_px = round(entry_px * (1.0 + sl_pct), 6)
            tp1_px = round(entry_px * (1.0 - tp1_pct), 6)
            tp2_px = round(entry_px * (1.0 - tp2_pct), 6)

        return {
            "symbol": sym,
            "last_price": last_px,
            "direction": direction,
            "order_type": "Limit",
            "auto_trade_enabled": False,
            "setup_type": setup_type,
            "grade": grade,
            "score": round(score, 1),
            "atr_15m_pct": atr_15m_pct,
            "sl_dist_pct": round(sl_pct * 100.0, 2),
            "oi_delta_45m_pct": round(oi_delta_pct, 2),
            "oi_value_m": round(oi_val / 1e6, 1),
            "funding_rate_pct": round(funding_rate_pct, 4),
            "obi_top5_pct": round(obi_pct, 1),
            "cvd_usd": round(cvd_usd, 0),
            "entry_price": entry_px,
            "tp1_price": tp1_px,
            "take_profit": tp2_px,
            "stop_loss": sl_px,
            "rr_ratio": 3.0,
            "rationale": rationale,
        }

    def scan_squeeze_setups(self, core_pairs: List[str], ws_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        tickers_data = self._curl_json("https://api.bybit.com/v5/market/tickers?category=linear")
        t_list = tickers_data.get("result", {}).get("list", [])
        t_map = {t.get("symbol"): t for t in t_list if t.get("symbol") in core_pairs}
        ws_metrics = {m.get("symbol"): m for m in ws_summary.get("metrics", [])}

        ranked_syms = sorted(
            [s for s in core_pairs if s in t_map],
            key=lambda s: abs(float(t_map[s].get("price24hPcnt", 0) or 0)) * 100.0
            + (1.5 if ws_metrics.get(s, {}).get("long_liq_usd", 0) + ws_metrics.get(s, {}).get("short_liq_usd", 0) > 0 else 0.0),
            reverse=True,
        )[:16]

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(self._scan_one, sym, t_map[sym], ws_metrics.get(sym, {})) for sym in ranked_syms]
            for f in as_completed(futs):
                try:
                    row = f.result()
                    if row:
                        results.append(row)
                except Exception:
                    continue

        results.sort(key=lambda x: x["score"], reverse=True)
        self.cached_signals = results
        self.last_scan_ts = time.strftime("%H:%M:%S")
        return results

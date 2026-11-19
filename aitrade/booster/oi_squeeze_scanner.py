# -*- coding: utf-8 -*-
from __future__ import annotations
import json, subprocess, time, urllib.request
from typing import Any, Dict, List, Tuple


class OISqueezeScanner:
    def __init__(self) -> None:
        self.last_scan_ts = "—"
        self.cached_signals: List[Dict[str, Any]] = []

    @staticmethod
    def _curl_json(url: str) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=6) as r: data=json.load(r)
            if not isinstance(data,dict) or data.get("retCode") != 0:
                raise RuntimeError("Ошибка рыночного API Bybit")
            return data
        except Exception as exc:
            raise RuntimeError("Нет подтверждённых данных рынка: " + type(exc).__name__) from None

    def _compute_15m_atr_and_anti_chase(self, sym: str, last_px: float) -> Tuple[float, bool, bool, str]:
        # Загружаем 16 свечей 15m для расчета реального ATR% и проверки растяжки свечи (Anti-High / Anti-Low)
        k_url = "https://api.bybit.com/v5/market/kline?category=linear&symbol=" + sym + "&interval=15&limit=16"
        k_resp = self._curl_json(k_url)
        k_list = k_resp.get("result", {}).get("list", [])
        if len(k_list) < 6 or last_px <= 0:
            raise RuntimeError("Недостаточно свечей для ATR: " + sym)

        # k_list[0] — текущая незакрытая 15m свеча, k_list[1] — предыдущая
        trs: List[float] = []
        for i in range(min(14, len(k_list) - 1)):
            hi = float(k_list[i][2] or last_px)
            lo = float(k_list[i][3] or last_px)
            prev_close = float(k_list[i + 1][4] or last_px)
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
            trs.append(tr / max(1e-9, last_px) * 100.0)

        atr_pct = sum(trs) / len(trs) if trs else 0.85
        atr_pct = max(0.45, min(2.50, atr_pct))

        # Проверка растяжки за последние 2 свечи (30 мин) и положения внутри 1-часового диапазона
        open_30m = float(k_list[1][1] or last_px)
        move_30m_pct = ((last_px - open_30m) / max(1e-9, open_30m)) * 100.0

        high_1h = max(float(k_list[i][2] or last_px) for i in range(min(4, len(k_list))))
        low_1h = min(float(k_list[i][3] or last_px) for i in range(min(4, len(k_list))))
        span_1h = max(1e-9, high_1h - low_1h)
        pos_in_1h = (last_px - low_1h) / span_1h  # 0.0 = на самом дне, 1.0 = на самом хае

        # Anti-High для LONG: если цена уже улетела вверх > max(1.55%, 1.85*ATR) И стоит в верхних 12% часового диапазона
        overbought_long = (move_30m_pct > max(1.55, atr_pct * 1.85)) and (pos_in_1h > 0.88)
        # Anti-Low для SHORT: если цена уже пролилась > -max(1.55%, 1.85*ATR) И стоит в нижних 12% диапазона
        oversold_short = (move_30m_pct < -max(1.55, atr_pct * 1.85)) and (pos_in_1h < 0.12)

        info_str = "ATR15m {:.2f}%, импульс 30м {:+.2f}%".format(atr_pct, move_30m_pct)
        return round(atr_pct, 2), overbought_long, oversold_short, info_str

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
        for sym in ranked_syms:
            tk = t_map.get(sym, {})
            last_px = float(tk.get("lastPrice", 0) or 0)
            if last_px <= 0:
                continue
            funding_rate_pct = float(tk.get("fundingRate", 0) or 0) * 100.0
            chg_24h_pct = float(tk.get("price24hPcnt", 0) or 0) * 100.0
            oi_val = float(tk.get("openInterestValue", 0) or 0)

            oi_url = "https://api.bybit.com/v5/market/open-interest?category=linear&symbol=" + sym + "&intervalTime=15min&limit=4"
            oi_resp = self._curl_json(oi_url)
            oi_items = oi_resp.get("result", {}).get("list", [])
            if len(oi_items) < 2: continue
            oi_delta_pct = 0.0
            if len(oi_items) >= 2:
                latest_oi = float(oi_items[0].get("openInterest", 0) or 0)
                prev_oi = float(oi_items[-1].get("openInterest", 0) or 0)
                if prev_oi > 0:
                    oi_delta_pct = ((latest_oi - prev_oi) / prev_oi) * 100.0

            atr_15m_pct, is_overbought_high, is_oversold_low, atr_note = self._compute_15m_atr_and_anti_chase(sym, last_px)

            wsm = ws_metrics.get(sym, {})
            obi_pct = float(wsm.get("obi_top5_pct", 0.0) or 0.0)
            cvd_usd = float(wsm.get("cvd_usd", 0.0) or 0.0)
            long_liq = float(wsm.get("long_liq_usd", 0.0) or 0.0)
            short_liq = float(wsm.get("short_liq_usd", 0.0) or 0.0)

            setup_type = "MONITOR"
            direction = "LONG"
            grade = "B"
            score = 50.0
            rationale = "Нейтральный баланс OI и фандинга"

            # 1) SHORT SQUEEZE A+ (с проверкой отсутствия покупки на самом хае и подтверждением OBI/CVD)
            if (oi_delta_pct >= 0.45 or short_liq > 200) and (funding_rate_pct < -0.0015 or (obi_pct >= 14.0 and cvd_usd >= 0)):
                direction = "LONG"
                if is_overbought_high:
                    setup_type = "WAIT PULLBACK (АНТИ-ХАЙ)"
                    score = 64.0
                    grade = "B"
                    rationale = "🛡 Анти-Хай блок: свеча уже перегрета вверх (" + atr_note + ") — ждем откат к VWAP"
                else:
                    setup_type = "SHORT SQUEEZE BREAKOUT"
                    score = min(96.0, 68.0 + max(0.0, oi_delta_pct * 4.5) + abs(min(0.0, funding_rate_pct * 400.0)) + max(0.0, obi_pct * 0.25))
                    grade = "A+" if (score >= 75.0 and obi_pct >= 8.0 and cvd_usd >= -500) else "A"
                    rationale = "OI {:+.2f}%, Funding {:+.4f}%, OBI {:+.1f}% | {}".format(oi_delta_pct, funding_rate_pct, obi_pct, atr_note)

            # 2) LIQUIDATION CASCADE REVERSAL
            elif long_liq > 500 and obi_pct >= 15.0 and cvd_usd >= 0:
                setup_type = "LIQ CASCADE REVERSAL"
                direction = "LONG"
                score = min(95.0, 74.0 + obi_pct * 0.35)
                grade = "A+"
                rationale = "Каскад лонг-ликвидаций (${:.0f}) выкуплен плитой OBI {:+.1f}% | {}".format(long_liq, obi_pct, atr_note)

            # 3) LONG SQUEEZE / DUMP (с проверкой отсутствия шорта на самом дне)
            elif (funding_rate_pct >= 0.012 or oi_delta_pct >= 0.45) and (obi_pct <= -14.0 and cvd_usd <= 0):
                direction = "SHORT"
                if is_oversold_low:
                    setup_type = "WAIT BOUNCE (АНТИ-ЛОУ)"
                    score = 63.0
                    grade = "B"
                    rationale = "🛡 Анти-Лоу блок: монета уже перепродана на дне (" + atr_note + ") — риск отскока"
                else:
                    setup_type = "LONG SQUEEZE / DUMP"
                    score = min(94.0, 67.0 + max(0.0, oi_delta_pct * 3.5) + abs(min(0.0, obi_pct * 0.3)))
                    grade = "A+" if (score >= 75.0 and obi_pct <= -10.0) else "A"
                    rationale = "Давление продавцов (OBI {:+.1f}%, Funding {:+.4f}%) | {}".format(obi_pct, funding_rate_pct, atr_note)

            else:
                direction = "LONG" if (obi_pct + chg_24h_pct) >= 0 else "SHORT"
                score = round(52.0 + min(18.0, abs(oi_delta_pct) * 3.0 + abs(obi_pct) * 0.2), 1)
                grade = "B"
                rationale = "OI {:+.2f}%, OBI {:+.1f}% | {}".format(oi_delta_pct, obi_pct, atr_note)

            # Адаптивный структурный стоп-лосс по волатильности: 1.8 * ATR(15m), в диапазоне [1.15% .. 2.60%]
            sl_pct = max(1.15, min(2.60, atr_15m_pct * 1.80)) / 100.0
            tp1_pct = sl_pct * 1.15
            tp2_pct = sl_pct * 2.50

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

            results.append({
                "symbol": sym,
                "last_price": last_px,
                "direction": direction,
                "order_type": "Limit",
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
                "rr_ratio": 2.5,
                "rationale": rationale,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        self.cached_signals = results
        self.last_scan_ts = time.strftime("%H:%M:%S")
        return results

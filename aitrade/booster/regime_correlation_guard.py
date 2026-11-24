# -*- coding: utf-8 -*-
from __future__ import annotations
import json, subprocess, time, urllib.request
from typing import Any, Dict, List, Tuple


class RegimeAndCorrelationGuard:
    SECTORS: Dict[str, str] = {
        "BTCUSDT": "MAJORS", "ETHUSDT": "MAJORS", "SOLUSDT": "MAJORS",
        "XRPUSDT": "MAJORS", "BNBUSDT": "MAJORS", "TRXUSDT": "MAJORS", "LTCUSDT": "MAJORS",
        "SUIUSDT": "L1_L2", "TONUSDT": "L1_L2", "ADAUSDT": "L1_L2",
        "AVAXUSDT": "L1_L2", "DOTUSDT": "L1_L2", "NEARUSDT": "L1_L2",
        "APTUSDT": "L1_L2", "SEIUSDT": "L1_L2", "TIAUSDT": "L1_L2", "ARBUSDT": "L1_L2", "OPUSDT": "L1_L2",
        "FETUSDT": "AI_DEPIN", "RENDERUSDT": "AI_DEPIN", "TAOUSDT": "AI_DEPIN",
        "WLDUSDT": "AI_DEPIN", "INJUSDT": "AI_DEPIN", "FILUSDT": "AI_DEPIN", "ICPUSDT": "AI_DEPIN",
        "LINKUSDT": "DEFI_RWA", "AAVEUSDT": "DEFI_RWA", "UNIUSDT": "DEFI_RWA",
        "ONDOUSDT": "DEFI_RWA", "ENAUSDT": "DEFI_RWA", "PENDLEUSDT": "DEFI_RWA", "JUPUSDT": "DEFI_RWA",
        "DOGEUSDT": "MEME", "1000PEPEUSDT": "MEME", "WIFUSDT": "MEME",
        "1000BONKUSDT": "MEME", "POPCATUSDT": "MEME", "ORDIUSDT": "MEME",
        "ZECUSDT": "PRIVACY",
    }

    def __init__(self) -> None:
        self.enabled: bool = True
        self.max_same_sector: int = 1
        self.max_same_direction_total: int = 2
        self.demo_lab_enabled: bool = False
        self.btc_dump_threshold_5m_pct: float = -1.25
        self.btc_pump_threshold_5m_pct: float = 1.25
        self.time_stop_enabled: bool = True
        self.time_stop_minutes: int = 45
        self.time_stop_band_pct: float = 0.25
        self.last_regime: Dict[str, Any] = {
            "state": "NEUTRAL",
            "label": "⚖️ BTC: NEUTRAL",
            "btc_price": 0.0,
            "btc_chg_5m_pct": 0.0,
            "btc_chg_15m_pct": 0.0,
            "btc_obi_pct": 0.0,
            "btc_cvd_usd": 0.0,
            "allow_long": True,
            "allow_short": True,
            "reason": "Рынок сбалансирован, разрешены оба направления",
        }
        self._last_fetch_ts: float = 0.0
        self._last_ok_ts: float = 0.0

    @staticmethod
    def _curl_json(url: str) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=6) as r: data=json.load(r)
            if not isinstance(data,dict) or data.get("retCode") != 0:
                raise RuntimeError("Ошибка рыночного API Bybit")
            return data
        except Exception as exc:
            raise RuntimeError("Нет подтверждённых данных рынка: " + type(exc).__name__) from None

    def get_sector(self, symbol: str) -> str:
        return self.SECTORS.get(symbol, "OTHER")

    def update_btc_regime(self, ws_summary: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        if now - self._last_fetch_ts < 8.0 and self.last_regime.get("btc_price", 0) > 0:
            return self.last_regime
        self._last_fetch_ts = now
        try:
            res = self._update_btc_regime_live(ws_summary)
            self._last_ok_ts = now
            return res
        except Exception:
            if self.last_regime.get("btc_price", 0) > 0 and (now - self._last_ok_ts) < 120.0:
                return self.last_regime
            ws_map = {m.get("symbol"): m for m in (ws_summary or {}).get("metrics", [])}
            btc_ws = ws_map.get("BTCUSDT", {})
            ws_px = float(btc_ws.get("mid_price", 0) or 0)
            if ws_px > 0:
                obi_pct = float(btc_ws.get("obi_top5_pct", 0.0) or 0.0)
                cvd_usd = float(btc_ws.get("cvd_usd", 0.0) or 0.0)
                self.last_regime = {
                    "state": "NEUTRAL",
                    "label": "⚖️ BTC: WS fallback",
                    "btc_price": round(ws_px, 2),
                    "btc_chg_5m_pct": 0.0,
                    "btc_chg_15m_pct": 0.0,
                    "btc_obi_pct": round(obi_pct, 1),
                    "btc_cvd_usd": round(cvd_usd, 0),
                    "allow_long": True,
                    "allow_short": True,
                    "reason": "REST недоступен — используется WS-поток BTCUSDT",
                }
                return self.last_regime
            raise

    def _update_btc_regime_live(self, ws_summary: Dict[str, Any]) -> Dict[str, Any]:

        k_url = "https://api.bybit.com/v5/market/kline?category=linear&symbol=BTCUSDT&interval=5&limit=4"
        k_data = self._curl_json(k_url)
        k_list = k_data.get("result", {}).get("list", [])

        if len(k_list) < 3: raise RuntimeError("Недостаточно свечей BTC")
        btc_px = 0.0
        chg_5m = 0.0
        chg_15m = 0.0
        if len(k_list) >= 3:
            cur_close = float(k_list[0][4] or 0)
            cur_open = float(k_list[0][1] or cur_close)
            prev_open_15m = float(k_list[2][1] or cur_open)
            btc_px = cur_close
            if cur_open > 0:
                chg_5m = ((cur_close - cur_open) / cur_open) * 100.0
            if prev_open_15m > 0:
                chg_15m = ((cur_close - prev_open_15m) / prev_open_15m) * 100.0

        ws_map = {m.get("symbol"): m for m in ws_summary.get("metrics", [])}
        btc_ws = ws_map.get("BTCUSDT", {})
        if btc_px <= 0:
            btc_px = float(btc_ws.get("mid_price", 0) or 0)
        obi_pct = float(btc_ws.get("obi_top5_pct", 0.0) or 0.0)
        cvd_usd = float(btc_ws.get("cvd_usd", 0.0) or 0.0)

        state = "NEUTRAL"
        label = "⚖️ BTC: NEUTRAL"
        allow_long = True
        allow_short = True
        reason = "Спокойная фаза BTC ({:+.2f}% за 5м)".format(chg_5m)

        if not self.enabled:
            label = "🔓 BTC Guard: ВЫКЛ"
        elif chg_5m <= self.btc_dump_threshold_5m_pct or (chg_15m <= -0.65 and (obi_pct < -8.0 or cvd_usd < 0)):
            state = "DUMP_GUARD"
            label = "🔴 BTC ПРОЛИВ ({:+.2f}% 5м) — Лонги БЛОК".format(chg_5m)
            allow_long = False
            allow_short = True
            reason = "Резкий пролив BTC ({:+.2f}% 5м, {:+.2f}% 15м, OBI {:+.1f}%) — защита от лонга альтов".format(chg_5m, chg_15m, obi_pct)
        elif chg_5m >= self.btc_pump_threshold_5m_pct or (chg_15m >= 0.65 and (obi_pct > 8.0 or cvd_usd > 0)):
            state = "PUMP_GUARD"
            label = "🟢 BTC ИМПУЛЬС ({:+.2f}% 5м) — Шорты БЛОК".format(chg_5m)
            allow_long = True
            allow_short = False
            reason = "Сильный импульс BTC ({:+.2f}% 5м, {:+.2f}% 15м, OBI {:+.1f}%) — запрет шорта против поводыря".format(chg_5m, chg_15m, obi_pct)
        elif chg_15m >= 0.25 and obi_pct >= 5.0:
            state = "BULL_TREND"
            label = "🟢 BTC: BULL ({:+.2f}% 15м)".format(chg_15m)
        elif chg_15m <= -0.25 and obi_pct <= -5.0:
            state = "BEAR_TREND"
            label = "🟠 BTC: BEAR ({:+.2f}% 15м)".format(chg_15m)
        else:
            state = "NEUTRAL"
            label = "⚖️ BTC: NEUTRAL ({:+.2f}% 5м)".format(chg_5m)

        self.last_regime = {
            "state": state,
            "label": label,
            "btc_price": round(btc_px, 2),
            "btc_chg_5m_pct": round(chg_5m, 2),
            "btc_chg_15m_pct": round(chg_15m, 2),
            "btc_obi_pct": round(obi_pct, 1),
            "btc_cvd_usd": round(cvd_usd, 0),
            "allow_long": allow_long,
            "allow_short": allow_short,
            "reason": reason,
        }
        return self.last_regime

    def can_open_position(
        self,
        symbol: str,
        side: str,
        open_positions: List[Dict[str, Any]],
        ws_summary: Dict[str, Any],
        strategy_id: str = "",
    ) -> Tuple[bool, str]:
        if not self.enabled:
            return True, "OK"

        is_t1_4h = any(k in str(strategy_id or "") for k in ("T1_DONCHIAN_TSMOM", "T3_KELTNER_TREND_4H"))
        if not is_t1_4h:
            regime = self.update_btc_regime(ws_summary)
            if side == "Buy" and not regime["allow_long"]:
                return False, "Блок BTC Guard: " + regime["reason"]
            if side == "Sell" and not regime["allow_short"]:
                return False, "Блок BTC Guard: " + regime["reason"]

        cand_sector = self.get_sector(symbol)
        same_dir_total = 0
        same_sector_dir = 0
        same_sector_syms: List[str] = []

        for p in open_positions:
            p_sym = str(p.get("symbol", ""))
            p_side = str(p.get("side", ""))
            if p_sym == symbol:
                return False, "Позиция по " + symbol + " уже открыта"
            if p_side == side:
                same_dir_total += 1
                if self.get_sector(p_sym) == cand_sector:
                    same_sector_dir += 1
                    same_sector_syms.append(p_sym)

        if self.demo_lab_enabled:
            eff_max_sector = max(6, self.max_same_sector)
            eff_max_dir = max(14, self.max_same_direction_total)
        else:
            eff_max_sector = max(2, self.max_same_sector) if is_t1_4h else self.max_same_sector
            eff_max_dir = max(4, self.max_same_direction_total) if is_t1_4h else self.max_same_direction_total

        if same_sector_dir >= eff_max_sector:
            return (
                False,
                "Блок Корреляции: сектор [" + cand_sector + "] уже занят в " + side + " (" + ", ".join(same_sector_syms) + ")",
            )

        if same_dir_total >= eff_max_dir:
            return (
                False,
                "Блок Корреляции: уже открыто макс. однонаправленных сделок (" + str(same_dir_total) + " в " + side + ")",
            )

        return True, "OK (" + cand_sector + (", 4h-TSMOM" if is_t1_4h else "") + ")"

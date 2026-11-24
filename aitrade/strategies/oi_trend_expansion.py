"""
B5_OI_TREND_EXPANSION — пробой диапазона 15m по тренду с подтверждением роста Open Interest.
В отличие от убыточного контр-тренда, входит ПО направлению пробоя при притоке новых позиций (ΔOI >= +1.2%)
и отсутствии перегретого фандинга против входа. Pure SL/TP (3.0R, до 12ч).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.base import TradeSignal


def generate_oi_trend_expansion_signal(
    symbol: str,
    bars_15m: List[Candle],
    oi_history: List[Dict[str, Any]],
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    min_oi_delta_pct: float = 1.2,
    tp_rr: float = 3.0,
    max_hold_hours: int = 12,
) -> Optional[TradeSignal]:
    if len(bars_15m) < 25 or not oi_history or len(oi_history) < 4:
        return None

    # Bybit возвращает open-interest history от новых к старым или наоборот — сортируем по timestamp
    parsed_oi = []
    for row in oi_history:
        try:
            ts = int(row.get("timestamp", 0) or 0)
            val = float(row.get("openInterest", 0) or 0)
            if val > 0:
                parsed_oi.append((ts, val))
        except Exception:
            continue
    if len(parsed_oi) < 4:
        return None
    parsed_oi.sort(key=lambda x: x[0])
    oi_now = parsed_oi[-1][1]
    oi_prev = parsed_oi[-4][1]  # ~45 минут назад (3 интервала по 15m)
    oi_delta_pct = ((oi_now - oi_prev) / max(oi_prev, 1e-9)) * 100.0
    if oi_delta_pct < min_oi_delta_pct:
        return None

    last_bar = bars_15m[-1]
    prev_bar = bars_15m[-2]
    prev_20 = bars_15m[-21:-1]
    hi_20 = max(c.high for c in prev_20)
    lo_20 = min(c.low for c in prev_20)
    prev_20_prior = bars_15m[-22:-2]
    hi_20_prev = max(c.high for c in prev_20_prior)
    lo_20_prev = min(c.low for c in prev_20_prior)

    side: Optional[str] = None
    if last_bar.close > hi_20 and prev_bar.close <= hi_20_prev and last_bar.close > last_bar.open:
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif last_bar.close < lo_20 and prev_bar.close >= lo_20_prev and last_bar.close < last_bar.open:
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    atr_15m = float(calc_atr_series(bars_15m, 14)[-1])
    if entry <= 0 or atr_15m <= 0:
        return None

    stop_dist = max(atr_15m * 1.4, entry * 0.0065)
    stop_pct = stop_dist / entry
    if stop_pct > 0.035:
        return None

    if side == "Buy":
        sl = entry - stop_dist
        tp = entry + stop_dist * tp_rr
    else:
        sl = entry + stop_dist
        tp = entry - stop_dist * tp_rr

    if sl <= 0 or tp <= 0:
        return None

    return TradeSignal(
        symbol=symbol,
        strategy_id="B5_OI_TREND_EXPANSION",
        strategy_name="OI Trend Breakout 15m (Demo Lab)",
        timeframe="15",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.46,
        rationale=[
            f"Пробой 20-барового диапазона 15m на росте Open Interest (ΔOI={oi_delta_pct:+.2f}% за 45м)",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "oi_delta_45m_pct": round(oi_delta_pct, 3),
            "atr_15m_pct": round((atr_15m / entry) * 100.0, 3),
        },
    )

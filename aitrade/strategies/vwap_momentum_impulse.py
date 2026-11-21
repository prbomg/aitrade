"""
B3_VWAP_MOMENTUM_IMPULSE — быстрый 15m импульсный пробой сессионного VWAP
с подтверждением объёма (RVOL >= 1.35) и 1h тренда (EMA20/EMA50).
Предназначен для активного набора живых сделок в Demo Lab.
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_ema, calc_session_vwap_bands
from aitrade.strategies.base import TradeSignal


def generate_vwap_momentum_signal(
    symbol: str,
    bars_15m: List[Candle],
    bars_1h: Optional[List[Candle]] = None,
    rvol: float = 1.0,
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    tp_rr: float = 2.5,
    sl_atr_mult: float = 1.3,
    max_hold_hours: int = 8,
) -> Optional[TradeSignal]:
    """
    Вход по импульсному выходу цены за сессионный VWAP (+/- 0.5σ) на повышенном объёме
    в сторону 1h тренда.
    """
    if len(bars_15m) < 40 or float(rvol or 0.0) < 1.35:
        return None

    vwap_info = calc_session_vwap_bands(bars_15m)
    vwap = float(getattr(vwap_info, "vwap", 0.0) or 0.0)
    upper_1 = float(getattr(vwap_info, "upper_1sd", 0.0) or 0.0)
    lower_1 = float(getattr(vwap_info, "lower_1sd", 0.0) or 0.0)
    if vwap <= 0 or upper_1 <= vwap or lower_1 >= vwap:
        return None

    sigma = max(float(getattr(vwap_info, "std", 0.0) or 0.0), upper_1 - vwap, vwap * 0.002)
    last_bar = bars_15m[-1]
    prev_bar = bars_15m[-2]
    atr_15m = calc_atr_series(bars_15m, 14)
    cur_atr = float(atr_15m[-1])
    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    if entry <= 0 or cur_atr <= 0:
        return None

    # 1h тренд-фильтр
    trend_bias = 0
    if bars_1h and len(bars_1h) >= 52:
        c1h = [c.close for c in bars_1h]
        e20 = calc_ema(c1h, 20)[-1]
        e50 = calc_ema(c1h, 50)[-1]
        trend_bias = 1 if e20 > e50 else (-1 if e20 < e50 else 0)
    else:
        c15 = [c.close for c in bars_15m]
        e20 = calc_ema(c15, 20)[-1]
        e50 = calc_ema(c15, 38)[-1]
        trend_bias = 1 if e20 > e50 else (-1 if e20 < e50 else 0)

    if trend_bias == 0:
        return None

    # Сигнал: пробой и закрепление за VWAP + 0.5σ (но не перекуплен > 2.2σ) на импульсной свече
    z_now = (last_bar.close - vwap) / sigma
    z_prev = (prev_bar.close - vwap) / sigma

    side: Optional[str] = None
    if trend_bias > 0 and 0.45 <= z_now <= 2.2 and z_prev < z_now and last_bar.close > last_bar.open:
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif trend_bias < 0 and -2.2 <= z_now <= -0.45 and z_prev > z_now and last_bar.close < last_bar.open:
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    stop_dist = max(cur_atr * sl_atr_mult, entry * 0.0065)
    stop_pct = stop_dist / entry
    if stop_pct > 0.035:
        return None

    if side == "Buy":
        sl = entry - stop_dist
        tp2 = entry + stop_dist * tp_rr
    else:
        sl = entry + stop_dist
        tp2 = entry - stop_dist * tp_rr

    if sl <= 0 or tp2 <= 0:
        return None

    return TradeSignal(
        symbol=symbol,
        strategy_id="B3_VWAP_MOMENTUM_IMPULSE",
        strategy_name="VWAP Momentum 15m (Demo Lab)",
        timeframe="15",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp2, 6),
        take_profit_2=round(tp2, 6),
        base_winrate_est=0.48,
        rationale=[
            f"Импульсный выход за сессионный VWAP (Z={z_now:+.2f}σ, RVOL={rvol:.2f}x) по 1h-тренду",
            f"SL={stop_pct * 100:.2f}%, TP={tp_rr:.1f}R, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "vwap_zscore": round(z_now, 3),
            "rvol": round(float(rvol), 3),
            "atr_15m_pct": round((cur_atr / entry) * 100.0, 3),
        },
    )

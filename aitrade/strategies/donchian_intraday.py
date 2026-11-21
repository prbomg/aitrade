"""
B2_DONCHIAN_INTRADAY — быстрый внутридневной пробой канала Дончиана (1h, 12/24 бара)
с фильтром направления по 4h EMA20/EMA50 и чистым Pure SL/TP (без раннего БУ).
Предназначен для ускорения частоты сделок в контуре Разгона / Demo Lab.
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_ema
from aitrade.strategies.base import TradeSignal


def generate_donchian_intraday_signal(
    symbol: str,
    bars_1h: List[Candle],
    bars_4h: Optional[List[Candle]] = None,
    reference_open_px: Optional[float] = None,
    funding_rate: float = 0.0,
    fast_window: int = 12,
    slow_window: int = 24,
    tp_rr: float = 3.0,
    sl_atr_mult: float = 1.5,
    max_hold_hours: int = 18,
) -> Optional[TradeSignal]:
    """
    Генерирует внутридневной сигнал пробоя 1h Donchian (12h и 24h) по направлению 4h-тренда.
    """
    if len(bars_1h) < max(slow_window + 5, 35):
        return None

    closes_1h = [c.close for c in bars_1h]
    atr_1h = calc_atr_series(bars_1h, 14)
    cur_atr = float(atr_1h[-1])
    last_bar = bars_1h[-1]
    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    if entry <= 0 or cur_atr <= 0:
        return None

    # 4h трендовый фильтр (EMA20 vs EMA50) — при нехватке 4h баров используем 1h EMA20/EMA50
    trend_bias = 0
    if bars_4h and len(bars_4h) >= 52:
        c4 = [c.close for c in bars_4h]
        e20 = calc_ema(c4, 20)[-1]
        e50 = calc_ema(c4, 50)[-1]
        trend_bias = 1 if e20 > e50 else (-1 if e20 < e50 else 0)
    elif len(closes_1h) >= 52:
        e20 = calc_ema(closes_1h, 20)[-1]
        e50 = calc_ema(closes_1h, 50)[-1]
        trend_bias = 1 if e20 > e50 else (-1 if e20 < e50 else 0)

    if trend_bias == 0:
        return None

    prev_fast = bars_1h[-(fast_window + 1) : -1]
    prev_slow = bars_1h[-(slow_window + 1) : -1]
    hi_fast = max(c.high for c in prev_fast)
    lo_fast = min(c.low for c in prev_fast)
    hi_slow = max(c.high for c in prev_slow)
    lo_slow = min(c.low for c in prev_slow)

    side: Optional[str] = None
    channel_tag = ""
    if trend_bias > 0 and last_bar.close > hi_fast and last_bar.close > last_bar.open:
        if funding_rate > 0.0005:
            return None
        side = "Buy"
        channel_tag = "24h" if last_bar.close > hi_slow else "12h"
    elif trend_bias < 0 and last_bar.close < lo_fast and last_bar.close < last_bar.open:
        if funding_rate < -0.0005:
            return None
        side = "Sell"
        channel_tag = "24h" if last_bar.close < lo_slow else "12h"

    if side is None:
        return None

    # Защита от слишком узкого или раздутого стопа
    stop_dist = max(cur_atr * sl_atr_mult, entry * 0.0075)
    stop_pct = stop_dist / entry
    if stop_pct > 0.045:
        return None

    if side == "Buy":
        sl = entry - stop_dist
        tp1 = entry + stop_dist * (tp_rr * 0.5)
        tp2 = entry + stop_dist * tp_rr
    else:
        sl = entry + stop_dist
        tp1 = entry - stop_dist * (tp_rr * 0.5)
        tp2 = entry - stop_dist * tp_rr

    if sl <= 0 or tp2 <= 0:
        return None

    return TradeSignal(
        symbol=symbol,
        strategy_id="B2_DONCHIAN_INTRADAY",
        strategy_name="Donchian 1h Breakout (Demo Lab)",
        timeframe="60",
        side=side,
        order_type="Limit",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp1, 6),
        take_profit_2=round(tp2, 6),
        base_winrate_est=0.45,
        rationale=[
            f"Пробой {channel_tag} канала Donchian (1h) по тренду 4h EMA20/50",
            f"SL={stop_pct * 100:.2f}% ({sl_atr_mult:.1f}x ATR), TP={tp_rr:.1f}R, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 1.0,
            "atr_1h_pct": round((cur_atr / entry) * 100.0, 3),
            "trend_bias_4h": float(trend_bias),
            "channel_window": 24.0 if channel_tag == "24h" else 12.0,
        },
    )

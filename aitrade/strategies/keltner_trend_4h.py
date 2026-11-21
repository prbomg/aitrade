"""
T3_KELTNER_TREND_4H — многодневный трендовый пробой каналов Кельтнера на 4h (напарник к T1).
Входит при выходе 4h-свечи за EMA20 ± 2.2*ATR(14) при ADX(14) >= 20.
Pure SL/TP (5.5R, удержание до 7 дней).
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_adx, calc_atr_series, calc_ema
from aitrade.strategies.base import TradeSignal


def generate_keltner_trend_4h_signal(
    symbol: str,
    bars_4h: List[Candle],
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    kc_mult: float = 2.2,
    tp_rr: float = 5.5,
    max_hold_hours: int = 168,
) -> Optional[TradeSignal]:
    if len(bars_4h) < 35:
        return None

    closes = [c.close for c in bars_4h]
    ema20 = calc_ema(closes, 20)
    atr14 = calc_atr_series(bars_4h, 14)
    adx_4h, _, _ = calc_adx(bars_4h, 14)
    if adx_4h < 20.0:
        return None

    last_bar = bars_4h[-1]
    prev_bar = bars_4h[-2]
    upper_now = ema20[-1] + kc_mult * atr14[-1]
    lower_now = ema20[-1] - kc_mult * atr14[-1]
    upper_prev = ema20[-2] + kc_mult * atr14[-2]
    lower_prev = ema20[-2] - kc_mult * atr14[-2]

    side: Optional[str] = None
    # Свежий выход из канала Кельтнера на закрытом 4h баре
    if last_bar.close > upper_now and prev_bar.close <= upper_prev and last_bar.close > last_bar.open:
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif last_bar.close < lower_now and prev_bar.close >= lower_prev and last_bar.close < last_bar.open:
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    cur_atr = float(atr14[-1])
    if entry <= 0 or cur_atr <= 0:
        return None

    stop_dist = max(cur_atr * 2.0, entry * 0.012)
    stop_pct = stop_dist / entry
    if stop_pct > 0.075:
        return None

    sl = entry - stop_dist if side == "Buy" else entry + stop_dist
    tp = entry + stop_dist * tp_rr if side == "Buy" else entry - stop_dist * tp_rr
    if sl <= 0 or tp <= 0:
        return None

    return TradeSignal(
        symbol=symbol,
        strategy_id="T3_KELTNER_TREND_4H",
        strategy_name="Keltner 4h Trend (Demo Lab)",
        timeframe="240",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.44,
        rationale=[
            f"Пробой 4h канала Кельтнера (EMA20 ± {kc_mult:.1f}x ATR, ADX4h={adx_4h:.1f})",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours // 24}д",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours // 24}д",
        features={
            "post_only": 0.0,
            "adx_4h": round(adx_4h, 2),
            "atr_4h_pct": round((cur_atr / entry) * 100.0, 3),
        },
    )

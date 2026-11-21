"""
B6_EMA_PULLBACK_TREND — вход на откате к EMA21 по направлению сильного 4h-тренда (15m/1h).
Ортогонален пробойным стратегиям: покупает коррекцию в аптренде (или продаёт отскок в даунтренде)
при развороте RSI(14) обратно по тренду. Pure SL/TP (3.0R, до 12ч).
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_adx, calc_atr_series, calc_ema, calc_rsi
from aitrade.strategies.base import TradeSignal


def generate_ema_pullback_signal(
    symbol: str,
    bars_15m: List[Candle],
    bars_1h: List[Candle],
    bars_4h: List[Candle],
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    tp_rr: float = 3.0,
    max_hold_hours: int = 12,
) -> Optional[TradeSignal]:
    if len(bars_15m) < 40 or len(bars_1h) < 30 or len(bars_4h) < 52:
        return None

    c4 = [c.close for c in bars_4h]
    e20_4h = calc_ema(c4, 20)[-1]
    e50_4h = calc_ema(c4, 50)[-1]
    adx_1h, _, _ = calc_adx(bars_1h, 14)
    if adx_1h < 20.0:
        return None

    trend_4h = 1 if e20_4h > e50_4h * 1.002 else (-1 if e20_4h < e50_4h * 0.998 else 0)
    if trend_4h == 0:
        return None

    c15 = [c.close for c in bars_15m]
    ema21 = calc_ema(c15, 21)[-1]
    rsi_s = calc_rsi(bars_15m, 14)
    rsi_now, rsi_prev = rsi_s[-1], rsi_s[-2]
    last_bar = bars_15m[-1]
    prev_bar = bars_15m[-2]
    atr_15m = float(calc_atr_series(bars_15m, 14)[-1])
    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    if entry <= 0 or atr_15m <= 0:
        return None

    # Касание/близость к EMA21 на текущей или прошлой свече + разворот RSI по тренду
    side: Optional[str] = None
    if (
        trend_4h > 0
        and (last_bar.low <= ema21 * 1.003 or prev_bar.low <= ema21 * 1.003)
        and last_bar.close > ema21
        and last_bar.close > last_bar.open
        and rsi_prev < 52.0
        and rsi_now > rsi_prev
    ):
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif (
        trend_4h < 0
        and (last_bar.high >= ema21 * 0.997 or prev_bar.high >= ema21 * 0.997)
        and last_bar.close < ema21
        and last_bar.close < last_bar.open
        and rsi_prev > 48.0
        and rsi_now < rsi_prev
    ):
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    stop_dist = max(atr_15m * 1.35, entry * 0.0070)
    stop_pct = stop_dist / entry
    if stop_pct > 0.035:
        return None

    sl = entry - stop_dist if side == "Buy" else entry + stop_dist
    tp = entry + stop_dist * tp_rr if side == "Buy" else entry - stop_dist * tp_rr
    if sl <= 0 or tp <= 0:
        return None

    return TradeSignal(
        symbol=symbol,
        strategy_id="B6_EMA_PULLBACK_TREND",
        strategy_name="EMA21 Pullback Trend 15m (Demo Lab)",
        timeframe="15",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.48,
        rationale=[
            f"Откат к EMA21 в 4h-тренде (ADX1h={adx_1h:.1f}, RSI {rsi_prev:.1f}->{rsi_now:.1f})",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "adx_1h": round(adx_1h, 2),
            "rsi_14": round(rsi_now, 2),
            "atr_15m_pct": round((atr_15m / entry) * 100.0, 3),
        },
    )

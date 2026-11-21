"""
B4_ORB_SESSION_BREAKOUT — пробой диапазона открытия торговой сессии (Opening Range Breakout, 15m).
Фиксирует диапазон первого часа Азии (00:00 UTC), Европы (07:00 UTC) или США (13:00 UTC)
и входит на пробой границы диапазона с подтверждением объёма. Pure SL/TP (2.5R, до 8ч).
"""

from __future__ import annotations

import datetime
from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.base import TradeSignal

SESSION_OPEN_HOURS_UTC = (0, 7, 13)


def generate_orb_session_signal(
    symbol: str,
    bars_15m: List[Candle],
    rvol: float = 1.0,
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    tp_rr: float = 2.5,
    max_hold_hours: int = 8,
) -> Optional[TradeSignal]:
    if len(bars_15m) < 24 or float(rvol or 0.0) < 1.15:
        return None

    last_bar = bars_15m[-1]
    prev_bar = bars_15m[-2]
    dt = datetime.datetime.fromtimestamp(last_bar.timestamp / 1000.0, tz=datetime.timezone.utc)

    # Находим ближайший старт сессии (00/07/13 UTC) в пределах последних 1..4 часов
    session_start_hour: Optional[int] = None
    for h in sorted(SESSION_OPEN_HOURS_UTC, reverse=True):
        if h + 1 <= dt.hour <= h + 4:
            session_start_hour = h
            break
    if session_start_hour is None:
        return None

    open_dt = dt.replace(hour=session_start_hour, minute=0, second=0, microsecond=0)
    or_start_ms = int(open_dt.timestamp() * 1000)
    or_end_ms = or_start_ms + 3_600_000  # первые 60 минут (4 свечи по 15m)

    or_bars = [c for c in bars_15m if or_start_ms <= c.timestamp < or_end_ms]
    if len(or_bars) < 3:
        return None

    or_high = max(c.high for c in or_bars)
    or_low = min(c.low for c in or_bars)
    or_range = or_high - or_low
    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    atr_15m = float(calc_atr_series(bars_15m, 14)[-1])
    if entry <= 0 or atr_15m <= 0 or or_range <= 0:
        return None

    # Пробой происходит именно на текущей свече (свежий выход из диапазона открытия)
    side: Optional[str] = None
    if last_bar.close > or_high and prev_bar.close <= or_high and last_bar.close > last_bar.open:
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif last_bar.close < or_low and prev_bar.close >= or_low and last_bar.close < last_bar.open:
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    stop_dist = max(min(or_range, atr_15m * 2.2), atr_15m * 1.2, entry * 0.0065)
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
        strategy_id="B4_ORB_SESSION_BREAKOUT",
        strategy_name="Session ORB 15m (Demo Lab)",
        timeframe="15",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.46,
        rationale=[
            f"Пробой диапазона открытия сессии {session_start_hour:02d}:00 UTC (OR={or_low:.4f}..{or_high:.4f}, RVOL={rvol:.2f}x)",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "session_open_utc": float(session_start_hour),
            "rvol": round(float(rvol), 3),
            "or_range_pct": round((or_range / entry) * 100.0, 3),
        },
    )

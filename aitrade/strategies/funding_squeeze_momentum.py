"""
B7_FUNDING_SQUEEZE_MOMENTUM — вход по импульсу против перегретого фандинга (1h).
Если толпа перегружена в шортах (funding <= -0.015%) и цена пробивает 4-часовой хай вверх —
вход в лонг на шорт-сквиз. Зеркально при перегретом лонг-фандинге (>= +0.035%) и пробое вниз.
Pure SL/TP (3.5R, до 18ч).
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.base import TradeSignal


def generate_funding_squeeze_signal(
    symbol: str,
    bars_1h: List[Candle],
    funding_rate: float,
    reference_open_px: Optional[float] = None,
    short_squeeze_funding: float = -0.00015,
    long_squeeze_funding: float = 0.00035,
    tp_rr: float = 3.5,
    max_hold_hours: int = 18,
) -> Optional[TradeSignal]:
    if len(bars_1h) < 20:
        return None

    fr = float(funding_rate or 0.0)
    last_bar = bars_1h[-1]
    prev_4h = bars_1h[-5:-1]
    hi_4h = max(c.high for c in prev_4h)
    lo_4h = min(c.low for c in prev_4h)

    side: Optional[str] = None
    if fr <= short_squeeze_funding and last_bar.close > hi_4h and last_bar.close > last_bar.open:
        side = "Buy"
    elif fr >= long_squeeze_funding and last_bar.close < lo_4h and last_bar.close < last_bar.open:
        side = "Sell"

    if side is None:
        return None

    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_bar.close)
    atr_1h = float(calc_atr_series(bars_1h, 14)[-1])
    if entry <= 0 or atr_1h <= 0:
        return None

    stop_dist = max(atr_1h * 1.5, entry * 0.0080)
    stop_pct = stop_dist / entry
    if stop_pct > 0.045:
        return None

    sl = entry - stop_dist if side == "Buy" else entry + stop_dist
    tp = entry + stop_dist * tp_rr if side == "Buy" else entry - stop_dist * tp_rr
    if sl <= 0 or tp <= 0:
        return None

    return TradeSignal(
        symbol=symbol,
        strategy_id="B7_FUNDING_SQUEEZE_MOMENTUM",
        strategy_name="Funding Squeeze Momentum 1h (Demo Lab)",
        timeframe="60",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.46,
        rationale=[
            f"Сквиз перегретого фандинга ({fr * 100:+.4f}%) + пробой 4h диапазона в сторону сквиза",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "funding_rate_bps": round(fr * 10000.0, 2),
            "atr_1h_pct": round((atr_1h / entry) * 100.0, 3),
        },
    )

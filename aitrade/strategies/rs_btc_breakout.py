"""
T2_RS_BTC_BREAKOUT — стратегия относительной силы альткоина против BTCUSDT (1h).
Ищет лидеров, опережающих BTC за 24ч на >= +2.2% (или аутсайдеров <= -2.2% для шорта)
с пробоем 12-часового экстремума. Pure SL/TP (4.0R, до 48ч).
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.base import TradeSignal


def generate_rs_btc_signal(
    symbol: str,
    bars_1h: List[Candle],
    btc_bars_15m: List[Candle],
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    min_rs_spread_pct: float = 2.2,
    tp_rr: float = 4.0,
    max_hold_hours: int = 48,
) -> Optional[TradeSignal]:
    if symbol == "BTCUSDT" or len(bars_1h) < 30 or len(btc_bars_15m) < 96:
        return None

    last_1h = bars_1h[-1]
    prev_24h = bars_1h[-25]
    if prev_24h.close <= 0 or btc_bars_15m[-96].close <= 0:
        return None

    alt_ret_24h = ((last_1h.close - prev_24h.close) / prev_24h.close) * 100.0
    btc_ret_24h = ((btc_bars_15m[-1].close - btc_bars_15m[-96].close) / btc_bars_15m[-96].close) * 100.0
    rs_spread = alt_ret_24h - btc_ret_24h

    prev_12h = bars_1h[-13:-1]
    hi_12h = max(c.high for c in prev_12h)
    lo_12h = min(c.low for c in prev_12h)

    side: Optional[str] = None
    if rs_spread >= min_rs_spread_pct and last_1h.close > hi_12h and last_1h.close > last_1h.open:
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif rs_spread <= -min_rs_spread_pct and last_1h.close < lo_12h and last_1h.close < last_1h.open:
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_1h.close)
    atr_1h = float(calc_atr_series(bars_1h, 14)[-1])
    if entry <= 0 or atr_1h <= 0:
        return None

    stop_dist = max(atr_1h * 1.6, entry * 0.0085)
    stop_pct = stop_dist / entry
    if stop_pct > 0.05:
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
        strategy_id="T2_RS_BTC_BREAKOUT",
        strategy_name="Relative Strength vs BTC (Demo Lab)",
        timeframe="60",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.45,
        rationale=[
            f"Относительная сила к BTC за 24ч: {rs_spread:+.2f}% (Альт {alt_ret_24h:+.2f}% vs BTC {btc_ret_24h:+.2f}%) + пробой 12h",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "rs_vs_btc_24h_pct": round(rs_spread, 3),
            "atr_1h_pct": round((atr_1h / entry) * 100.0, 3),
        },
    )

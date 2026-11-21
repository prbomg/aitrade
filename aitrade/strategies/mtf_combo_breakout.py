"""
C2_MTF_COMBO_BREAKOUT — комбинированная мультифакторная стратегия (ансамбль):
объединяет 4h тренд (EMA20/50) + относительную силу против BTC (24h) + позицию относительно
сессионного VWAP + пробой 8-часового диапазона на 1h. Pure SL/TP (4.5R, до 36ч).
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_ema, calc_session_vwap_bands
from aitrade.strategies.base import TradeSignal


def generate_mtf_combo_signal(
    symbol: str,
    bars_15m: List[Candle],
    bars_1h: List[Candle],
    bars_4h: List[Candle],
    btc_bars_15m: List[Candle],
    funding_rate: float = 0.0,
    reference_open_px: Optional[float] = None,
    tp_rr: float = 4.5,
    max_hold_hours: int = 36,
) -> Optional[TradeSignal]:
    if len(bars_15m) < 30 or len(bars_1h) < 30 or len(bars_4h) < 52 or len(btc_bars_15m) < 96:
        return None

    # Фактор 1: 4h тренд (EMA20 vs EMA50)
    c4 = [c.close for c in bars_4h]
    trend_4h = 1 if calc_ema(c4, 20)[-1] > calc_ema(c4, 50)[-1] else -1

    # Фактор 2: Относительная сила к BTC за 24ч (для BTCUSDT считаем 0)
    alt_ret_24h = ((bars_1h[-1].close - bars_1h[-25].close) / max(bars_1h[-25].close, 1e-9)) * 100.0
    btc_ret_24h = ((btc_bars_15m[-1].close - btc_bars_15m[-96].close) / max(btc_bars_15m[-96].close, 1e-9)) * 100.0
    rs_spread = 0.0 if symbol == "BTCUSDT" else (alt_ret_24h - btc_ret_24h)

    # Фактор 3: Позиция относительно сессионного VWAP
    vwap_info = calc_session_vwap_bands(bars_15m)
    vwap = float(getattr(vwap_info, "vwap", 0.0) or 0.0)
    if vwap <= 0:
        return None

    # Фактор 4: Пробой 8-часового канала на 1h
    last_1h = bars_1h[-1]
    prev_8h = bars_1h[-9:-1]
    hi_8h = max(c.high for c in prev_8h)
    lo_8h = min(c.low for c in prev_8h)

    side: Optional[str] = None
    if (
        trend_4h > 0
        and rs_spread >= 0.8
        and last_1h.close > vwap
        and last_1h.close > hi_8h
        and last_1h.close > last_1h.open
    ):
        if funding_rate > 0.0005:
            return None
        side = "Buy"
    elif (
        trend_4h < 0
        and rs_spread <= -0.8
        and last_1h.close < vwap
        and last_1h.close < lo_8h
        and last_1h.close < last_1h.open
    ):
        if funding_rate < -0.0005:
            return None
        side = "Sell"

    if side is None:
        return None

    entry = float(reference_open_px if (reference_open_px and reference_open_px > 0) else last_1h.close)
    atr_1h = float(calc_atr_series(bars_1h, 14)[-1])
    if entry <= 0 or atr_1h <= 0:
        return None

    stop_dist = max(atr_1h * 1.5, entry * 0.0080)
    stop_pct = stop_dist / entry
    if stop_pct > 0.045:
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
        strategy_id="C2_MTF_COMBO_BREAKOUT",
        strategy_name="MTF Combo (4h+RS+VWAP+8h)",
        timeframe="60",
        side=side,
        order_type="Market",
        entry_price=round(entry, 6),
        stop_loss=round(sl, 6),
        take_profit_1=round(tp, 6),
        take_profit_2=round(tp, 6),
        base_winrate_est=0.48,
        rationale=[
            f"Комбо-совпадение: 4h тренд + RS vs BTC {rs_spread:+.2f}% + выше/ниже VWAP + пробой 8h",
            f"Pure SL/TP ({tp_rr:.1f}R), SL={stop_pct * 100:.2f}%, Hold<={max_hold_hours}ч",
        ],
        invalidation_rule=f"Pure SL/TP ({tp_rr:.1f}R) или закрытие через {max_hold_hours}ч",
        features={
            "post_only": 0.0,
            "rs_vs_btc_24h_pct": round(rs_spread, 3),
            "trend_4h": float(trend_4h),
            "atr_1h_pct": round((atr_1h / entry) * 100.0, 3),
        },
    )

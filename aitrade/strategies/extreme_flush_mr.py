# -*- coding: utf-8 -*-
"""
Кандидат для диверсификации Автотрейдинга — MR1_EXTREME_FLUSH (Контр-тренд после экстремального выноса, 1h):
- В отличие от S3_VWAP_RANGE (который торговал обычный шум на 15m и сливал с PF=0.32),
  MR1 входит ТОЛЬКО после статистически экстремального импульса/каскада на 1h барах:
  * |Z-score(close, 24h)| >= z_threshold (по умолчанию 2.4σ),
  * Экстремальный RSI(14) (<= 28 для Long, >= 72 для Short),
  * Признак истощения / выкупа хвоста свечи (rejection wick),
  * Фильтр фандинга против перегретой толпы.
- Широкий стоп SL = max(1.5%, 2.0 * ATR_1h), цель TP2 = 2.2R, удержание до 12–18ч.
"""

from __future__ import annotations

import math
from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_rsi
from aitrade.strategies.base import TradeSignal


def generate_extreme_flush_mr_signal(
    symbol: str,
    bars_1h: List[Candle],
    reference_open_px: Optional[float] = None,
    funding_rate: float = 0.0,
    z_threshold: float = 2.4,
    tp_rr: float = 2.2,
    max_hold_bars: int = 12,
    require_wick: bool = True,
) -> Optional[TradeSignal]:
    """Генерирует контр-трендовый сигнал MR1_EXTREME_FLUSH строго по закрытым 1h барам."""
    if len(bars_1h) < 35:
        return None

    last = bars_1h[-1]
    entry_px = float(reference_open_px if reference_open_px and reference_open_px > 0 else last.close)
    if entry_px <= 0:
        return None

    # Z-score закрытия относительно предыдущих 24 закрытых 1h баров (без утечки)
    ref_closes = [b.close for b in bars_1h[-25:-1]]
    mean_c = sum(ref_closes) / len(ref_closes)
    var_c = sum((c - mean_c) ** 2 for c in ref_closes) / max(len(ref_closes) - 1, 1)
    std_c = math.sqrt(max(var_c, 1e-12))
    z_score = (last.close - mean_c) / std_c

    rsi_series = calc_rsi(bars_1h[-30:], period=14)
    rsi_14 = float(rsi_series[-1]) if isinstance(rsi_series, list) and rsi_series else float(rsi_series or 50.0)
    atr_1h = max(calc_atr_series(bars_1h[-30:], 14)[-1], entry_px * 0.005)
    bar_rng = max(last.high - last.low, 1e-12)
    lower_wick_ratio = (min(last.open, last.close) - last.low) / bar_rng
    upper_wick_ratio = (last.high - max(last.open, last.close)) / bar_rng

    side: Optional[str] = None
    if z_score <= -z_threshold and rsi_14 <= 30.0 and funding_rate <= 0.0002:
        if (not require_wick) or lower_wick_ratio >= 0.25 or ((last.close - last.low) / bar_rng >= 0.35):
            side = "Buy"
    elif z_score >= z_threshold and rsi_14 >= 70.0 and funding_rate >= -0.0001:
        if (not require_wick) or upper_wick_ratio >= 0.25 or ((last.high - last.close) / bar_rng >= 0.35):
            side = "Sell"

    if side is None:
        return None

    sl_dist = max(entry_px * 0.015, 2.0 * atr_1h)
    sl_px = entry_px - sl_dist if side == "Buy" else entry_px + sl_dist
    tp1 = entry_px + 1.4 * sl_dist if side == "Buy" else entry_px - 1.4 * sl_dist
    tp2 = entry_px + tp_rr * sl_dist if side == "Buy" else entry_px - tp_rr * sl_dist

    return TradeSignal(
        symbol=symbol,
        strategy_id="MR1_EXTREME_FLUSH",
        strategy_name=f"MR1: Extreme Flush Reversion 1h ({'Long' if side == 'Buy' else 'Short'})",
        timeframe="60",
        side=side,
        order_type="Market",
        entry_price=entry_px,
        stop_loss=sl_px,
        take_profit_1=tp1,
        take_profit_2=tp2,
        base_winrate_est=0.48,
        rationale=[
            f"Z24h={z_score:+.2f}σ",
            f"RSI14={rsi_14:.1f}",
            f"Funding={funding_rate * 100:.4f}%",
        ],
        invalidation_rule=f"SL = max(1.5%, 2.0*ATR1h); TP2={tp_rr:.1f}R; MaxHold={max_hold_bars}h",
        features={
            "z_score_24h": round(z_score, 3),
            "rsi_14": round(rsi_14, 2),
            "funding_rate": float(funding_rate),
            "atr1h": float(atr_1h),
            "post_only": 0.0,
            "max_hold_bars": float(max_hold_bars),
        },
    )

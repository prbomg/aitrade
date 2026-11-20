# -*- coding: utf-8 -*-
"""
Кандидат для Модуля «Автотрейдинг» — Multi-Horizon Donchian Channel + TSMOM Ensemble (4h):
- Эмпирическое основание: Concretum Group / Swiss Finance Institute No. 25-80 (ансамбль каналов Дончиана
  на нескольких горизонтах + волатильностное масштабирование позиций на топ-ликвидных крипто-активах).
- Работает строго на закрытых 4h барах (`bars_4h`), без заглядывания в будущее.
- Ансамбль 3 горизонтов (по умолчанию 20 / 40 / 80 баров 4h ≈ 3.3 / 6.7 / 13.3 суток).
- Вход при согласованном пробое ансамбля + выравнивании EMA20/EMA50 + фильтре перегретого фандинга.
- Широкий волатильностный SL (`max(1.5%, 2.5 * ATR_4h)`), цели `TP1 = 2.0R`, `TP2 = 4.0R`,
  удержание до 30 баров 4h (5 суток) — позволяет ловить трендовые хвосты и переживать 2x издержки.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_ema
from aitrade.strategies.base import TradeSignal


def aggregate_4h(candles: List[Candle]) -> List[Candle]:
    """Агрегирует 15m/1h свечи в строго закрытые 4h бары по UTC-границам (4 * 3600s)."""
    if not candles:
        return []
    step_ms = (candles[1].timestamp - candles[0].timestamp) if len(candles) > 1 else 900_000
    if step_ms >= 14_400_000:
        return list(candles)

    four_h_ms = 14_400_000
    expected_sub = max(1, int(round(four_h_ms / max(60_000, step_ms))))
    buckets: Dict[int, List[Candle]] = {}
    for c in candles:
        b_ts = (int(c.timestamp) // four_h_ms) * four_h_ms
        buckets.setdefault(b_ts, []).append(c)

    out: List[Candle] = []
    for b_ts in sorted(buckets.keys()):
        grp = buckets[b_ts]
        if len(grp) < expected_sub:
            continue
        out.append(
            Candle(
                timestamp=b_ts,
                open=grp[0].open,
                high=max(x.high for x in grp),
                low=min(x.low for x in grp),
                close=grp[-1].close,
                volume=sum(x.volume for x in grp),
                turnover=sum(x.turnover for x in grp),
            )
        )
    return out


def calc_realized_ann_vol_4h(bars_4h: List[Candle], lookback: int = 42) -> float:
    """Годовая реализованная волатильность по 4h лог-доходностям (6 баров/день * 365)."""
    if len(bars_4h) < lookback + 1:
        return 0.60
    window = bars_4h[-(lookback + 1) :]
    rets: List[float] = []
    for i in range(1, len(window)):
        p0, p1 = window[i - 1].close, window[i].close
        if p0 > 0 and p1 > 0:
            rets.append(math.log(p1 / p0))
    if len(rets) < 10:
        return 0.60
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1)
    ann_vol = math.sqrt(max(var_r, 1e-10)) * math.sqrt(6.0 * 365.0)
    return max(0.15, min(3.0, ann_vol))


def generate_donchian_tsmom_signal(
    symbol: str,
    bars_4h: List[Candle],
    reference_open_px: Optional[float] = None,
    funding_rate: float = 0.0,
    horizons: Sequence[int] = (20, 40, 80),
    target_ann_vol: float = 0.25,
    max_hold_bars: int = 30,
) -> Optional[TradeSignal]:
    """
    Генерирует тренд-фолловинг сигнал `T1_DONCHIAN_TSMOM` строго по закрытым 4h барам.
    - Вход по `reference_open_px` (Open следующего 4h бара) или `bars_4h[-1].close` в live.
    """
    max_h = max(horizons) if horizons else 80
    if len(bars_4h) < max_h + 5:
        return None

    last = bars_4h[-1]
    prev = bars_4h[-2]
    entry_px = float(reference_open_px if reference_open_px and reference_open_px > 0 else last.close)
    if entry_px <= 0:
        return None

    closes = [b.close for b in bars_4h]
    ema20 = calc_ema(closes, 20)[-1]
    ema50 = calc_ema(closes, 50)[-1]
    atr_4h = max(calc_atr_series(bars_4h, 14)[-1], entry_px * 0.006)
    ann_vol = calc_realized_ann_vol_4h(bars_4h, lookback=42)
    vol_scale = max(0.35, min(1.80, target_ann_vol / max(ann_vol, 0.15)))

    votes: List[float] = []
    fresh_long_breakout = False
    fresh_short_breakdown = False

    for h in horizons:
        # Канал строится строго по барам ДО последнего закрытого бара (без утечки)
        chan = bars_4h[-(h + 1) : -1]
        hi = max(b.high for b in chan)
        lo = min(b.low for b in chan)
        mid = 0.5 * (hi + lo)
        ref_past_close = chan[0].close

        if last.close > hi:
            votes.append(1.0)
            if prev.close <= hi:
                fresh_long_breakout = True
        elif last.close < lo:
            votes.append(-1.0)
            if prev.close >= lo:
                fresh_short_breakdown = True
        elif last.close > mid and last.close > ref_past_close:
            votes.append(0.5)
        elif last.close < mid and last.close < ref_past_close:
            votes.append(-0.5)
        else:
            votes.append(0.0)

    ens_score = sum(votes) / max(len(votes), 1)
    side: Optional[str] = None
    if ens_score >= 0.65 and fresh_long_breakout and ema20 > ema50 and funding_rate <= 0.0006:
        side = "Buy"
    elif ens_score <= -0.65 and fresh_short_breakdown and ema20 < ema50 and funding_rate >= -0.0006:
        side = "Sell"

    if side is None:
        return None

    sl_dist = max(entry_px * 0.015, 2.5 * atr_4h)
    sl_px = entry_px - sl_dist if side == "Buy" else entry_px + sl_dist
    tp1 = entry_px + 2.0 * sl_dist if side == "Buy" else entry_px - 2.0 * sl_dist
    tp2 = entry_px + 4.0 * sl_dist if side == "Buy" else entry_px - 4.0 * sl_dist

    return TradeSignal(
        symbol=symbol,
        strategy_id="T1_DONCHIAN_TSMOM",
        strategy_name=f"Автотрейдинг T1: Donchian TSMOM 4h ({'Long' if side == 'Buy' else 'Short'})",
        timeframe="240",
        side=side,
        order_type="Market",
        entry_price=entry_px,
        stop_loss=sl_px,
        take_profit_1=tp1,
        take_profit_2=tp2,
        base_winrate_est=0.45,
        rationale=[
            f"DonchianEnsemble={ens_score:+.2f} ({'/'.join(str(x) for x in horizons)})",
            f"EMA20/50={'Bull' if ema20 > ema50 else 'Bear'}",
            f"AnnVol={ann_vol * 100:.1f}% VolScale={vol_scale:.2f}x",
        ],
        invalidation_rule=f"SL = max(1.5%, 2.5*ATR4h); MaxHold={max_hold_bars * 4}h",
        features={
            "ens_score": round(ens_score, 3),
            "ann_vol": round(ann_vol, 4),
            "vol_scale": round(vol_scale, 3),
            "funding_rate": float(funding_rate),
            "atr4h": float(atr_4h),
            "post_only": 0.0,
            "max_hold_bars": float(max_hold_bars),
        },
    )

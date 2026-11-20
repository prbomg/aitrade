# -*- coding: utf-8 -*-
"""
Ядро B1 Модуля «Разгон» — Volatility Squeeze + Supertrend Breakout (15m / 1h):
- Режим 1h строго TREND_UP или TREND_DOWN (из regime_router.classify_regime).
- Предварительное сжатие: Bollinger(20, 2.0) внутри Keltner(20, 1.8*ATR) минимум на 2 из последних 5 баров.
- Пробой закрытой свечой за границу канала в направлении Supertrend(1h) и EMA20/EMA50(1h).
- Подтверждение объёма: RVOL >= 1.35.
- Фильтр фандинга: блок Buy при funding > +0.03%/8h, блок Sell при funding < -0.03%/8h.
- Геометрия: SL >= max(1.2% [1.0% мейджоры / 1.5% альты], 1.5*ATR15), TP1=+1.8R, TP2=+3.2R.
"""

from __future__ import annotations

from typing import List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import (
    calc_atr_series,
    calc_bollinger_keltner_squeeze,
    calc_rvol,
)
from aitrade.strategies.base import TradeSignal
from aitrade.strategies.regime_router import classify_regime, has_recent_gaps
from aitrade.strategies.regime_s1_s3 import (
    MAJORS,
    _build_ml_features,
    aggregate_1h,
)


def generate_booster_breakout_signal(
    symbol: str,
    closed_15m: List[Candle],
    reference_open_px: Optional[float] = None,
    bars_1h: Optional[List[Candle]] = None,
    funding_rate: Optional[float] = None,
    obi_smoothed: Optional[float] = None,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    tick_size: float = 0.0,
    use_hmm: bool = False,
) -> Optional[TradeSignal]:
    """Генерирует сигнал Ядра B1 (Volatility Squeeze + Supertrend Breakout) по закрытым свечам."""
    if len(closed_15m) < 60 or has_recent_gaps(closed_15m, step_ms=900_000, check_bars=60):
        return None

    h1_series = bars_1h if (bars_1h is not None and len(bars_1h) >= 55) else aggregate_1h(closed_15m)
    decision = classify_regime(h1_series, use_hmm=use_hmm)
    if decision.regime not in ("TREND_UP", "TREND_DOWN"):
        return None

    # Проверка сжатия BB внутри Keltner на >= 2 из последних 5 баров до пробойной свечи
    tail_15 = closed_15m[-65:]
    squeeze_hits = 0
    for offset in range(2, 7):
        sub = tail_15[:-offset] if offset > 0 else tail_15
        if len(sub) < 25:
            continue
        _, _, _, _, _, is_sq = calc_bollinger_keltner_squeeze(sub, period=20, bb_mult=2.0, kc_mult=1.8)
        if is_sq:
            squeeze_hits += 1
    if squeeze_hits < 2:
        return None

    bb_u, bb_l, kc_u, kc_l, ema20_15, _ = calc_bollinger_keltner_squeeze(
        tail_15[:-1], period=20, bb_mult=2.0, kc_mult=1.8
    )
    c_sig = closed_15m[-1]
    rvol = calc_rvol(tail_15[-25:], lookback=20)
    if rvol < 1.35:
        return None

    atr15 = max(calc_atr_series(tail_15, 14)[-1], c_sig.close * 0.002)
    ref_px = reference_open_px if (reference_open_px and reference_open_px > 0) else c_sig.close

    side: Optional[str] = None
    entry_px = 0.0
    sl_px = 0.0
    min_sl_frac = 0.010 if symbol in MAJORS else 0.015

    if decision.regime == "TREND_UP" and decision.supertrend_dir == 1:
        if funding_rate is not None and funding_rate > 0.0003:
            return None
        breakout_level = max(bb_u, kc_u)
        if c_sig.close > breakout_level and c_sig.close > ema20_15:
            side = "Buy"
            passive_cap = (best_bid - tick_size) if (best_bid and best_bid > 0) else (ref_px * 0.9995)
            entry_px = min(ref_px, passive_cap)
            sl_dist = max(entry_px * min_sl_frac, 1.5 * atr15)
            sl_px = entry_px - sl_dist

    elif decision.regime == "TREND_DOWN" and decision.supertrend_dir == -1:
        if funding_rate is not None and funding_rate < -0.0003:
            return None
        breakout_level = min(bb_l, kc_l)
        if c_sig.close < breakout_level and c_sig.close < ema20_15:
            side = "Sell"
            passive_floor = (best_ask + tick_size) if (best_ask and best_ask > 0) else (ref_px * 1.0005)
            entry_px = max(ref_px, passive_floor)
            sl_dist = max(entry_px * min_sl_frac, 1.5 * atr15)
            sl_px = entry_px + sl_dist

    if side is None or entry_px <= 0:
        return None

    risk_dist = abs(entry_px - sl_px)
    if risk_dist <= 0:
        return None
    tp1 = entry_px + 1.8 * risk_dist if side == "Buy" else entry_px - 1.8 * risk_dist
    tp2 = entry_px + 3.2 * risk_dist if side == "Buy" else entry_px - 3.2 * risk_dist

    feats = _build_ml_features(closed_15m, obi_smoothed=obi_smoothed)
    feats.update({
        "atr15": atr15,
        "squeeze_hits": float(squeeze_hits),
        "adx_1h": decision.adx_14,
        "atr_pct_1h": decision.atr_percentile,
        "hurst_1h": decision.hurst,
        "funding_rate": float(funding_rate or 0.0),
        "post_only": 1.0,
        "retest_ttl_bars": 3.0,
        "max_hold_bars": 72.0,
    })

    return TradeSignal(
        symbol=symbol,
        strategy_id="B1_SQUEEZE_BREAKOUT",
        strategy_name="B1: Volatility Squeeze + Supertrend Breakout",
        timeframe="15",
        side=side,
        order_type="Limit",
        entry_price=entry_px,
        stop_loss=sl_px,
        take_profit_1=tp1,
        take_profit_2=tp2,
        base_winrate_est=0.42,
        rationale=[
            f"1h={decision.regime}",
            f"Squeeze={squeeze_hits}/5",
            f"RVOL={rvol:.2f}",
            "Supertrend aligned",
        ],
        invalidation_rule="Pure SL/TP (без раннего БУ/трейлинга): SL >= 1.0-1.5% / 1.5*ATR; TP2=+3.2R; max_hold=72 бара (18ч)",
        features=feats,
    )

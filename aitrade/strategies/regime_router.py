# -*- coding: utf-8 -*-
"""
Единый режимный гейт AItrade 2026 (заменяет шумный порог er20 >= 0.22):
- Числовая тройка на закрытых 1h барах: ADX(14), ATR_percentile(100), Hurst(100).
- Проверка непрерывности таймстемпов только на последних 60 барах.
- Опциональная верификация через 3-состоячную GaussianHMM (P(state) >= 0.60).
- Чёткая маршрутизация:
  * TREND_UP / TREND_DOWN -> BOOSTER (B1/B2), AUTOTRADE_MOMENTUM (Рукав B)
  * RANGE -> AUTOTRADE_MR (Рукав A)
  * NO_REGIME (серая зона) -> только AUTOTRADE_CARRY (Рукав C)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import (
    calc_adx,
    calc_atr_percentile,
    calc_ema,
    calc_hurst_exponent,
    calc_supertrend,
)
from aitrade.ml.regime_hmm import HMMRegimeResult, RegimeHMM


@dataclass
class RegimeDecision:
    regime: str  # TREND_UP | TREND_DOWN | RANGE | NO_REGIME
    adx_14: float
    atr_percentile: float
    hurst: float
    supertrend_dir: int
    ema20: float
    ema50: float
    hmm_state: str = "NO_REGIME"
    hmm_confidence: float = 0.0
    hmm_backend: str = "unavailable"
    allowed_modules: List[str] = field(default_factory=list)
    reason: str = ""


def has_recent_gaps(candles: List[Candle], step_ms: int, check_bars: int = 60) -> bool:
    """Проверяет непрерывность таймстемпов строго на последних `check_bars` барах."""
    if len(candles) < 2:
        return False
    tail = candles[-min(len(candles), check_bars) :]
    return any((b.timestamp - a.timestamp) != step_ms for a, b in zip(tail, tail[1:]))


_REGIME_CACHE: Dict[Tuple[int, float, int, int, bool], RegimeDecision] = {}


def classify_regime(
    bars_1h: List[Candle],
    use_hmm: bool = False,
    hmm_model: Optional[RegimeHMM] = None,
) -> RegimeDecision:
    """
    Классифицирует 1h режим рынка по числовой тройке (ADX, ATR%, Hurst)
    с проверкой направления по EMA20/EMA50 и Supertrend(10, 3.0).
    """
    if len(bars_1h) < 55:
        return RegimeDecision(
            regime="NO_REGIME",
            adx_14=0.0,
            atr_percentile=50.0,
            hurst=0.5,
            supertrend_dir=0,
            ema20=0.0,
            ema50=0.0,
            allowed_modules=["AUTOTRADE_CARRY"],
            reason="Недостаточно 1h баров (<55)",
        )

    if has_recent_gaps(bars_1h, step_ms=3_600_000, check_bars=60):
        return RegimeDecision(
            regime="NO_REGIME",
            adx_14=0.0,
            atr_percentile=50.0,
            hurst=0.5,
            supertrend_dir=0,
            ema20=0.0,
            ema50=0.0,
            allowed_modules=["AUTOTRADE_CARRY"],
            reason="Пропуск свечей в последних 60 барах 1h",
        )

    cache_key = None
    if hmm_model is None:
        cache_key = (int(bars_1h[-1].timestamp), float(bars_1h[-1].close), int(bars_1h[0].timestamp), len(bars_1h), bool(use_hmm))
        cached = _REGIME_CACHE.get(cache_key)
        if cached is not None:
            return cached

    closes = [c.close for c in bars_1h]
    adx_val, plus_di, minus_di = calc_adx(bars_1h, 14)
    atr_pct = calc_atr_percentile(bars_1h, atr_period=14, lookback=100)
    hurst = calc_hurst_exponent(closes, window=100)
    _, st_dirs = calc_supertrend(bars_1h, period=10, multiplier=3.0)
    st_dir = st_dirs[-1] if st_dirs else 0
    ema20 = calc_ema(closes, 20)[-1]
    ema50 = calc_ema(closes, 50)[-1]

    # Голоса числовой тройки: минимум 2 из 3 при отсутствии прямого конфликта третьего
    trend_votes = int(adx_val > 25.0) + int(atr_pct > 60.0) + int(hurst > 0.55)
    range_votes = int(adx_val < 20.0) + int(atr_pct < 40.0) + int(hurst < 0.45)

    regime = "NO_REGIME"
    reason = f"Серая зона: ADX={adx_val:.1f}, ATR%={atr_pct:.1f}, H={hurst:.2f}"

    if trend_votes >= 2 and range_votes == 0:
        if ema20 > ema50 and st_dir == 1:
            regime = "TREND_UP"
            reason = f"Тренд вверх: ADX={adx_val:.1f}, ATR%={atr_pct:.1f}, H={hurst:.2f}, ST=+1"
        elif ema20 < ema50 and st_dir == -1:
            regime = "TREND_DOWN"
            reason = f"Тренд вниз: ADX={adx_val:.1f}, ATR%={atr_pct:.1f}, H={hurst:.2f}, ST=-1"
        else:
            regime = "NO_REGIME"
            reason = f"Конфликт направления EMA20/50 и Supertrend (ST={st_dir})"
    elif range_votes >= 2 and trend_votes == 0:
        regime = "RANGE"
        reason = f"Флэт: ADX={adx_val:.1f}, ATR%={atr_pct:.1f}, H={hurst:.2f}"

    hmm_res = HMMRegimeResult("NO_REGIME", 0.0, "unavailable")
    if use_hmm:
        hmm = hmm_model or RegimeHMM()
        hmm_res = hmm.predict(bars_1h)
        if hmm_res.backend == "gaussian_hmm":
            if hmm_res.confidence < RegimeHMM.CONFIDENCE_GATE:
                regime = "NO_REGIME"
                reason = f"HMM confidence {hmm_res.confidence:.2f} < {RegimeHMM.CONFIDENCE_GATE}"
            elif regime == "TREND_UP" and hmm_res.state != "BULL_TREND":
                regime = "NO_REGIME"
                reason = f"Конфликт с HMM ({hmm_res.state})"
            elif regime == "TREND_DOWN" and hmm_res.state != "BEAR_TREND":
                regime = "NO_REGIME"
                reason = f"Конфликт с HMM ({hmm_res.state})"
            elif regime == "RANGE" and hmm_res.state != "SIDEWAYS":
                regime = "NO_REGIME"
                reason = f"Конфликт с HMM ({hmm_res.state})"

    if regime in ("TREND_UP", "TREND_DOWN"):
        allowed = ["BOOSTER", "AUTOTRADE_MOMENTUM", "AUTOTRADE_CARRY"]
    elif regime == "RANGE":
        allowed = ["AUTOTRADE_MR", "AUTOTRADE_CARRY"]
    else:
        allowed = ["AUTOTRADE_CARRY"]

    dec = RegimeDecision(
        regime=regime,
        adx_14=round(adx_val, 2),
        atr_percentile=round(atr_pct, 2),
        hurst=round(hurst, 4),
        supertrend_dir=st_dir,
        ema20=round(ema20, 6),
        ema50=round(ema50, 6),
        hmm_state=hmm_res.state,
        hmm_confidence=hmm_res.confidence,
        hmm_backend=hmm_res.backend,
        allowed_modules=allowed,
        reason=reason,
    )
    if cache_key is not None:
        if len(_REGIME_CACHE) >= 4096:
            _REGIME_CACHE.clear()
        _REGIME_CACHE[cache_key] = dec
    return dec

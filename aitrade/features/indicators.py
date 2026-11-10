"""
Математическое ядро расчета индикаторов на чистом Python (Zero-Dependency):
- EMA (8, 21, 50, 200) и SMA (20)
- ATR(14), ATR_baseline(20), NATR (%) и фильтр расширения волатильности ATR_14 / ATR_20
- RVOL (Relative Volume за N периодов)
- RSI(14) и ADX(14) (+DI / -DI) для переключения режимов (Range vs Trend)
- Полосы Боллинджера (20 SMA ± 2σ), каналы Кельтнера (20 EMA ± 2×ATR) и детектор Volatility Squeeze
- Институциональный сессионный VWAP (якорь 00:00 UTC) с полосами стандартного отклонения (±1σ, ±2σ, ±3σ)
- Приближенный профиль объема сессии: POC (Point of Control), VAH (Value Area High), VAL (Value Area Low)
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from typing import List, Tuple

from aitrade.exchange.bybit_v5 import Candle


@dataclass
class VWAPBands:
    vwap: float
    std: float
    upper_1sd: float
    lower_1sd: float
    upper_2sd: float
    lower_2sd: float
    upper_3sd: float
    lower_3sd: float
    slope_pct: float


@dataclass
class VolumeProfileZones:
    poc: float
    vah: float
    val: float


def calc_sma(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    out: List[float] = []
    running_sum = 0.0
    for i, val in enumerate(values):
        running_sum += val
        if i >= period:
            running_sum -= values[i - period]
            out.append(running_sum / period)
        else:
            out.append(running_sum / (i + 1))
    return out


def calc_ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out: List[float] = [values[0]]
    for val in values[1:]:
        out.append(alpha * val + (1.0 - alpha) * out[-1])
    return out


def calc_true_range(candles: List[Candle]) -> List[float]:
    if not candles:
        return []
    tr_list: List[float] = [candles[0].high - candles[0].low]
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1].close
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        tr_list.append(tr)
    return tr_list


def calc_atr_series(candles: List[Candle], period: int = 14) -> List[float]:
    tr = calc_true_range(candles)
    if not tr:
        return []
    out: List[float] = [tr[0]]
    alpha = 1.0 / max(1, period)
    for val in tr[1:]:
        out.append(alpha * val + (1.0 - alpha) * out[-1])
    return out


def calc_natr_and_ratio(candles: List[Candle]) -> Tuple[float, float, float]:
    """
    Возвращает (ATR_14, NATR_pct, ATR_expansion_ratio = ATR_14 / ATR_baseline_20).
    Согласно исследованию VWAP 2026 года, при ATR_14 / ATR_20 > 1.5 входы блокируются.
    """
    if not candles:
        return 0.0, 0.0, 1.0
    atr_14_series = calc_atr_series(candles, 14)
    atr_14 = atr_14_series[-1]
    atr_baseline_20 = sum(atr_14_series[-20:]) / min(len(atr_14_series), 20)
    last_close = max(candles[-1].close, 1e-9)
    natr_pct = (atr_14 / last_close) * 100.0
    expansion_ratio = atr_14 / max(atr_baseline_20, 1e-9)
    return atr_14, natr_pct, expansion_ratio


def calc_rvol(candles: List[Candle], lookback: int = 20) -> float:
    """
    Относительный объем RVOL = Текущий объем / Средний объем за предыдущие N свечей.
    Использует последнюю закрытую или активную свечу в сравнении с исторической нормой.
    """
    if len(candles) < 3:
        return 1.0
    hist = candles[-(lookback + 1) : -1]
    avg_vol = sum(c.volume for c in hist) / max(len(hist), 1)
    if avg_vol <= 0:
        return 1.0
    # Берем максимум из последней и предпоследней свечи (чтобы не занижать RVOL в первые секунды новой свечи)
    current_vol = max(candles[-1].volume, candles[-2].volume)
    return current_vol / avg_vol


def calc_rsi(candles: List[Candle], period: int = 14) -> List[float]:
    if len(candles) < 2:
        return [50.0] * len(candles)
    gains: List[float] = [0.0]
    losses: List[float] = [0.0]
    for i in range(1, len(candles)):
        diff = candles[i].close - candles[i - 1].close
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / max(min(len(gains), period), 1)
    avg_loss = sum(losses[:period]) / max(min(len(losses), period), 1)
    rsi_out: List[float] = []
    for i in range(len(candles)):
        if i < period:
            g = sum(gains[: i + 1]) / (i + 1)
            l = sum(losses[: i + 1]) / (i + 1)
        else:
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            g, l = avg_gain, avg_loss
        if l <= 1e-12:
            rsi_out.append(100.0 if g > 0 else 50.0)
        else:
            rs = g / l
            rsi_out.append(100.0 - (100.0 / (1.0 + rs)))
    return rsi_out


def calc_adx(candles: List[Candle], period: int = 14) -> Tuple[float, float, float]:
    """Возвращает (ADX_14, +DI_14, -DI_14)."""
    if len(candles) < period + 2:
        return 18.0, 20.0, 20.0
    tr_list = calc_true_range(candles)
    plus_dm: List[float] = [0.0]
    minus_dm: List[float] = [0.0]
    for i in range(1, len(candles)):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
        plus_dm.append(pdm)
        minus_dm.append(mdm)

    atr_s = calc_ema(tr_list, period)
    pdm_s = calc_ema(plus_dm, period)
    mdm_s = calc_ema(minus_dm, period)

    dx_list: List[float] = []
    plus_di = 20.0
    minus_di = 20.0
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a <= 1e-12:
            dx_list.append(0.0)
            continue
        plus_di = 100.0 * (p / a)
        minus_di = 100.0 * (m / a)
        denom = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / denom if denom > 1e-12 else 0.0
        dx_list.append(dx)

    adx_series = calc_ema(dx_list, period)
    return adx_series[-1], plus_di, minus_di


def calc_bollinger_keltner_squeeze(
    candles: List[Candle], period: int = 20, bb_mult: float = 2.0, kc_mult: float = 2.0
) -> Tuple[float, float, float, float, float, bool]:
    """
    Возвращает (bb_upper, bb_lower, kc_upper, kc_lower, midline_ema20, is_squeeze_on).
    Volatility Squeeze фиксируется, когда полосы Боллинджера (20 SMA ± 2σ) входят внутрь каналов Кельтнера (20 EMA ± 2×ATR).
    """
    if not candles:
        return 0.0, 0.0, 0.0, 0.0, 0.0, False
    closes = [c.close for c in candles]
    sma20 = calc_sma(closes, period)[-1]
    ema20 = calc_ema(closes, period)[-1]
    window = closes[-period:]
    mean_w = sum(window) / len(window)
    variance = sum((x - mean_w) ** 2 for x in window) / max(len(window), 1)
    std_dev = math.sqrt(variance)

    atr_val = calc_atr_series(candles, 14)[-1]
    bb_upper = sma20 + bb_mult * std_dev
    bb_lower = sma20 - bb_mult * std_dev
    kc_upper = ema20 + kc_mult * atr_val
    kc_lower = ema20 - kc_mult * atr_val

    is_squeeze = (bb_upper <= kc_upper) and (bb_lower >= kc_lower)
    return bb_upper, bb_lower, kc_upper, kc_lower, ema20, is_squeeze


def calc_session_vwap_bands(candles: List[Candle]) -> VWAPBands:
    """
    Рассчитывает институциональный сессионный VWAP с якорем на открытие текущего дня 00:00 UTC
    (или последние 32 свечи, если сессия только началась) по типичной цене hlc3 = (H + L + C) / 3
    и строит полосы стандартного отклонения ±1σ, ±2σ, ±3σ.
    """
    if not candles:
        return VWAPBands(0, 0, 0, 0, 0, 0, 0, 0, 0)

    last_dt = datetime.datetime.fromtimestamp(candles[-1].timestamp / 1000.0, tz=datetime.timezone.utc)
    day_anchor_dt = last_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    anchor_ms = int(day_anchor_dt.timestamp() * 1000)

    session_candles = [c for c in candles if c.timestamp >= anchor_ms]
    if len(session_candles) < 8:
        session_candles = candles[-min(len(candles), 32) :]

    cum_pv = 0.0
    cum_vol = 0.0
    cum_pv2 = 0.0
    vwap_history: List[float] = []

    for c in session_candles:
        tp = (c.high + c.low + c.close) / 3.0
        vol = max(c.volume, 1e-9)
        cum_pv += tp * vol
        cum_vol += vol
        cum_pv2 += (tp ** 2) * vol
        curr_vwap = cum_pv / cum_vol
        vwap_history.append(curr_vwap)

    vwap = vwap_history[-1]
    variance = max(0.0, (cum_pv2 / cum_vol) - (vwap ** 2))
    std = math.sqrt(variance)
    # Если дисперсия в начале сессии слишком мала, берем минимум 0.35% от цены
    min_std = vwap * 0.0035
    std = max(std, min_std)

    lookback_idx = max(0, len(vwap_history) - 5)
    prev_vwap = vwap_history[lookback_idx]
    slope_pct = ((vwap - prev_vwap) / max(prev_vwap, 1e-9)) * 100.0

    return VWAPBands(
        vwap=vwap,
        std=std,
        upper_1sd=vwap + 1.0 * std,
        lower_1sd=vwap - 1.0 * std,
        upper_2sd=vwap + 2.0 * std,
        lower_2sd=vwap - 2.0 * std,
        upper_3sd=vwap + 3.0 * std,
        lower_3sd=vwap - 3.0 * std,
        slope_pct=slope_pct,
    )


def calc_volume_profile(candles: List[Candle], bins: int = 24) -> VolumeProfileZones:
    """
    Строит внутридневной профиль объема (Volume Profile) и находит:
    - POC (Point of Control) — уровень с максимальным проторгованным объемом,
    - VAH (Value Area High) и VAL (Value Area Low), охватывающие 70% объема сессии.
    """
    if not candles:
        return VolumeProfileZones(0.0, 0.0, 0.0)
    subset = candles[-min(len(candles), 64) :]
    low_min = min(c.low for c in subset)
    high_max = max(c.high for c in subset)
    if high_max <= low_min:
        p = subset[-1].close
        return VolumeProfileZones(p, p * 1.005, p * 0.995)

    step = (high_max - low_min) / bins
    vol_bins = [0.0] * bins
    bin_prices = [low_min + (i + 0.5) * step for i in range(bins)]

    for c in subset:
        tp = (c.high + c.low + c.close) / 3.0
        idx = int((tp - low_min) / step)
        idx = max(0, min(bins - 1, idx))
        vol_bins[idx] += c.volume

    poc_idx = max(range(bins), key=lambda i: vol_bins[i])
    total_vol = sum(vol_bins)
    target_vol = total_vol * 0.70

    accum_vol = vol_bins[poc_idx]
    left = poc_idx
    right = poc_idx
    while accum_vol < target_vol and (left > 0 or right < bins - 1):
        left_vol = vol_bins[left - 1] if left > 0 else -1.0
        right_vol = vol_bins[right + 1] if right < bins - 1 else -1.0
        if right_vol >= left_vol and right < bins - 1:
            right += 1
            accum_vol += vol_bins[right]
        elif left > 0:
            left -= 1
            accum_vol += vol_bins[left]
        else:
            break

    return VolumeProfileZones(
        poc=bin_prices[poc_idx],
        vah=bin_prices[right] + 0.5 * step,
        val=bin_prices[left] - 0.5 * step,
    )


def determine_mtf_bias(candles_1h: List[Candle]) -> Tuple[str, float, float, float]:
    """
    Правило из документа: 'Старший таймфрейм (1H) определяет направление (Bias) с помощью веера
    экспоненциальных скользящих средних EMA 8 / 21 / 50:
    - разделенные и направленные линии -> Trend (BULLISH_TREND / BEARISH_TREND)
    - переплетенные и горизонтальные -> Range (RANGE)
    - пересекающиеся в середине резкого разворота -> Transition (TRANSITION)'.
    """
    if len(candles_1h) < 10:
        return "RANGE", 0.0, 0.0, 0.0
    closes = [c.close for c in candles_1h]
    ema8 = calc_ema(closes, 8)[-1]
    ema21 = calc_ema(closes, 21)[-1]
    ema50 = calc_ema(closes, 50)[-1]
    adx_val, plus_di, minus_di = calc_adx(candles_1h, 14)

    spread_pct = abs(ema8 - ema50) / max(ema50, 1e-9) * 100.0
    if spread_pct < 0.28 or adx_val < 20.0:
        return "RANGE", ema8, ema21, ema50

    if ema8 > ema21 > ema50 and plus_di >= minus_di * 0.9:
        return "BULLISH_TREND", ema8, ema21, ema50
    if ema8 < ema21 < ema50 and minus_di >= plus_di * 0.9:
        return "BEARISH_TREND", ema8, ema21, ema50

    return "TRANSITION", ema8, ema21, ema50

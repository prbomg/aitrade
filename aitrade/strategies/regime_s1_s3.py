"""
Единый модуль стратегий S1 (пробой с ретестом в тренде) и S3 (возврат к VWAP ±2σ во флэте)
с 1h-роутером режима, UTC-агрегацией, сессионным VWAP с 00:00 UTC и полными ML-признаками.
Используется одновременно в боевом AITradeCombineEngine и в историческом WalkForwardBacktester.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import (
    calc_atr_series,
    calc_ema,
    calc_rsi,
    calc_rvol,
    calc_session_vwap_bands,
)
from aitrade.strategies.base import TradeSignal

MIN_STOP_FRAC = 0.0055  # 0.55%: защита от съедания комиссиями Bybit


def aggregate_1h(closed_15m: List[Candle]) -> List[Candle]:
    """Агрегирует закрытые 15m свечи в 1h бары строго по границам часа UTC (без сдвига)."""
    buckets: Dict[int, List[Candle]] = {}
    for c in closed_15m:
        hour_ts = (int(c.timestamp) // 3_600_000) * 3_600_000
        buckets.setdefault(hour_ts, []).append(c)

    bars_1h: List[Candle] = []
    for hour_ts in sorted(buckets.keys()):
        chunk = sorted(buckets[hour_ts], key=lambda x: x.timestamp)
        # Принимаем только полный 1h-бар из 4 последовательных 15m свечей (00, 15, 30, 45)
        if len(chunk) != 4:
            continue
        if [int(c.timestamp) for c in chunk] != [hour_ts+j*900_000 for j in range(4)]:
            continue
        bars_1h.append(
            Candle(
                timestamp=hour_ts,
                open=chunk[0].open,
                high=max(c.high for c in chunk),
                low=min(c.low for c in chunk),
                close=chunk[-1].close,
                volume=sum(c.volume for c in chunk),
                turnover=sum(c.turnover for c in chunk),
            )
        )
    return bars_1h


def classify_regime_1h(bars_1h: List[Candle]) -> str:
    """Определяет 1h режим: TREND_UP, TREND_DOWN, RANGE или TRANSITION."""
    if len(bars_1h) < 55 or any(b.timestamp-a.timestamp != 3_600_000 for a,b in zip(bars_1h[-55:],bars_1h[-54:])):
        return "TRANSITION"
    closes = [c.close for c in bars_1h]
    ema20 = calc_ema(closes, 20)[-1]
    ema50 = calc_ema(closes, 50)[-1]
    atr14 = max(calc_atr_series(bars_1h, 14)[-1], closes[-1] * 0.002)

    net_move = abs(closes[-1] - closes[-21])
    path_sum = sum(abs(closes[k] - closes[k - 1]) for k in range(len(closes) - 20, len(closes)))
    er20 = net_move / max(path_sum, 1e-9)
    sep = abs(ema20 - ema50) / atr14

    if er20 >= 0.22 and sep >= 0.25:
        return "TREND_UP" if ema20 > ema50 else "TREND_DOWN"
    if er20 < 0.22 and sep <= 0.85:
        return "RANGE"
    return "TRANSITION"


def _session_candles_utc(closed_15m: List[Candle]) -> List[Candle]:
    """Возвращает свечи текущей сессии с 00:00 UTC (минимум 16 баров с фоллбэком)."""
    if not closed_15m:
        return []
    last_ts = int(closed_15m[-1].timestamp)
    day_start = (last_ts // 86_400_000) * 86_400_000
    session = [c for c in closed_15m if int(c.timestamp) >= day_start]
    return session


def _build_ml_features(closed_15m: List[Candle], obi_smoothed: Optional[float] = None) -> Dict[str, float]:
    """Вычисляет согласованный набор признаков для WalkForwardMetaLabeler (R06)."""
    sess = _session_candles_utc(closed_15m)
    vwap_b = calc_session_vwap_bands(sess)
    rsi_val = calc_rsi(closed_15m[-40:], 14)[-1]
    rvol_val = calc_rvol(closed_15m[-25:], lookback=20)
    c_sig = closed_15m[-1]
    z_vwap = (c_sig.close - vwap_b.vwap) / max(vwap_b.std, 1e-9)
    rng = max(c_sig.high - c_sig.low, 1e-9)
    clv_proxy = ((c_sig.close - c_sig.low) - (c_sig.high - c_sig.close)) / rng
    obi_val = obi_smoothed if obi_smoothed is not None else clv_proxy
    return {
        "vwap_zscore": round(z_vwap, 4),
        "rsi_14": round(rsi_val, 2),
        "rvol": round(rvol_val, 3),
        "obi_smoothed": round(max(-1.0, min(1.0, obi_val)), 4),
        "clv_proxy": round(clv_proxy,4),
        "vwap": vwap_b.vwap,
    }


def generate_s1_s3_signal(
    symbol: str,
    closed_15m: List[Candle],
    reference_open_px: Optional[float] = None,
    bars_1h: Optional[List[Candle]] = None,
    min_rr_s3: float = 0.50,
    obi_smoothed: Optional[float] = None,
) -> Optional[TradeSignal]:
    """
    Генерирует торговый сигнал S1 или S3 строго по закрытым свечам closed_15m.
    Возвращает None, если режим TRANSITION или нет сетапа.
    """
    if len(closed_15m) < 48 or any(b.timestamp-a.timestamp != 900_000 for a,b in zip(closed_15m,closed_15m[1:])):
        return None

    h1_series = bars_1h if (bars_1h is not None and len(bars_1h) >= 55) else aggregate_1h(closed_15m)
    regime = classify_regime_1h(h1_series)
    if regime == "TRANSITION":
        return None

    atr_s = calc_atr_series(closed_15m, 14)
    c_sig = closed_15m[-1]
    atr15 = max(atr_s[-1], c_sig.close * 0.002)

    if regime in ("TREND_UP", "TREND_DOWN"):
        lookback = closed_15m[-13:-1]
        if len(lookback) < 12:
            return None
        box_high = max(c.high for c in lookback)
        box_low = min(c.low for c in lookback)
        avg_vol = sum(c.volume for c in closed_15m[-21:-1]) / 20.0
        if c_sig.volume < 1.1 * avg_vol:
            return None

        side: Optional[str] = None
        retest_px = 0.0
        sl_px = 0.0
        if regime == "TREND_UP" and c_sig.close > box_high:
            side = "Buy"
            retest_px = box_high + 0.25 * atr15
            sl_px = min(
                retest_px * (1.0 - MIN_STOP_FRAC),
                retest_px - 1.2 * atr15,
                c_sig.low - 0.25 * atr15,
            )
        elif regime == "TREND_DOWN" and c_sig.close < box_low:
            side = "Sell"
            retest_px = box_low - 0.25 * atr15
            sl_px = max(
                retest_px * (1.0 + MIN_STOP_FRAC),
                retest_px + 1.2 * atr15,
                c_sig.high + 0.25 * atr15,
            )

        if side is None:
            return None
        risk_dist = abs(retest_px - sl_px)
        if risk_dist <= 0:
            return None
        tp1 = retest_px + 1.5 * risk_dist if side == "Buy" else retest_px - 1.5 * risk_dist
        tp2 = retest_px + 2.0 * risk_dist if side == "Buy" else retest_px - 2.0 * risk_dist

        feats = _build_ml_features(closed_15m, obi_smoothed=obi_smoothed)
        feats.update({
            "atr15": atr15,
            "box_high": box_high,
            "box_low": box_low,
            "retest_ttl_bars": 3.0,
            "max_hold_bars": 24.0,
        })

        return TradeSignal(
            symbol=symbol,
            strategy_id="S1_TREND_RETEST",
            strategy_name="S1: Пробой 15m с ретестом (1h Trend)",
            timeframe="15",
            side=side,
            order_type="Limit",
            entry_price=retest_px,
            stop_loss=sl_px,
            take_profit_1=tp1,
            take_profit_2=tp2,
            base_winrate_est=0.48,
            rationale=[f"1h={regime}", f"Breakout {'high' if side == 'Buy' else 'low'}", "Retest Limit"],
            invalidation_rule="Закрытие за SL; после +1.0R перенос в БУ; max_hold=24 бара",
            features=feats,
        )

    # RANGE -> S3_VWAP_RANGE (сессионный VWAP с 00:00 UTC)
    sess = _session_candles_utc(closed_15m)
    if len(sess)<8:return None
    vwap_b = calc_session_vwap_bands(sess)
    entry_px = reference_open_px if reference_open_px and reference_open_px > 0 else c_sig.close
    side = None
    sl_px = 0.0
    prev_bar = closed_15m[-2]
    closes_15m = [c.close for c in closed_15m[-60:]]
    ema20_15m = calc_ema(closes_15m, 20)[-1]
    ema50_15m = calc_ema(closes_15m, min(50, len(closes_15m)))[-1]
    no_waterfall = not (ema20_15m < ema50_15m * 0.997)
    no_meltup = not (ema20_15m > ema50_15m * 1.003)
    if c_sig.low <= vwap_b.lower_2sd and c_sig.close > c_sig.open and c_sig.close > prev_bar.high and no_waterfall and entry_px < vwap_b.vwap:
        side = "Buy"
        sl_px = min(entry_px * (1.0 - MIN_STOP_FRAC), c_sig.low - 0.6 * atr15)
    elif c_sig.high >= vwap_b.upper_2sd and c_sig.close < c_sig.open and c_sig.close < prev_bar.low and no_meltup and entry_px > vwap_b.vwap:
        side = "Sell"
        sl_px = max(entry_px * (1.0 + MIN_STOP_FRAC), c_sig.high + 0.6 * atr15)

    if side is None:
        return None
    risk_dist = abs(entry_px - sl_px)
    reward_dist = abs(vwap_b.vwap - entry_px)
    if risk_dist <= 0 or (reward_dist / risk_dist) < min_rr_s3:
        return None

    feats = _build_ml_features(closed_15m, obi_smoothed=obi_smoothed)
    feats.update({
        "atr15": atr15,
        "vwap": vwap_b.vwap,
        "max_hold_bars": 16.0,
    })

    return TradeSignal(
        symbol=symbol,
        strategy_id="S3_VWAP_RANGE",
        strategy_name="S3: Возврат к VWAP ±2σ (1h Range)",
        timeframe="15",
        side=side,
        order_type="Market",
        entry_price=entry_px,
        stop_loss=sl_px,
        take_profit_1=vwap_b.vwap,
        take_profit_2=vwap_b.vwap,
        base_winrate_est=0.56,
        rationale=[f"1h={regime}", "VWAP ±2σ rejection", "Target VWAP"],
        invalidation_rule="Фиксированный выход по VWAP или SL; max_hold=16 баров без раннего БУ",
        features=feats,
    )

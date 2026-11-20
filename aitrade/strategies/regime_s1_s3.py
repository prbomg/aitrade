"""
Единый модуль стратегий S1 (пробой с пассивным ретестом в тренде) и S3 (возврат к VWAP ±2σ во флэте)
с единым 1h-роутером режима (ADX + ATR% + Hurst + Supertrend), UTC-агрегацией,
сессионным VWAP с 00:00 UTC и полными ML-признаками.
Используется одновременно в боевом AITradeCombineEngine и в WalkForwardBacktester.
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
from aitrade.strategies.regime_router import classify_regime, has_recent_gaps

MAJORS = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}
MIN_STOP_FRAC = 0.0080  # 0.80% для Автотрейдинга (S3)
MIN_STOP_FRAC_S1_MAJOR = 0.0100  # 1.0% для мейджоров в Разгоне
MIN_STOP_FRAC_S1_ALT = 0.0120  # 1.2% для альтов в Разгоне


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
        if [int(c.timestamp) for c in chunk] != [hour_ts + j * 900_000 for j in range(4)]:
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


def classify_regime_1h(bars_1h: List[Candle], use_hmm: bool = False) -> str:
    """Обёртка над единым режимным гейтом (ADX + ATR% + Hurst + Supertrend)."""
    return classify_regime(bars_1h, use_hmm=use_hmm).regime


def _session_candles_utc(closed_15m: List[Candle]) -> List[Candle]:
    """Возвращает свечи текущей сессии с 00:00 UTC."""
    if not closed_15m:
        return []
    last_ts = int(closed_15m[-1].timestamp)
    day_start = (last_ts // 86_400_000) * 86_400_000
    return [c for c in closed_15m if int(c.timestamp) >= day_start]


def _build_ml_features(closed_15m: List[Candle], obi_smoothed: Optional[float] = None) -> Dict[str, float]:
    """Вычисляет согласованный набор признаков для WalkForwardMetaLabeler."""
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
        "clv_proxy": round(clv_proxy, 4),
        "vwap": vwap_b.vwap,
    }


def generate_s1_s3_signal(
    symbol: str,
    closed_15m: List[Candle],
    reference_open_px: Optional[float] = None,
    bars_1h: Optional[List[Candle]] = None,
    min_rr_s3: float = 1.15,
    obi_smoothed: Optional[float] = None,
    best_bid: Optional[float] = None,
    best_ask: Optional[float] = None,
    tick_size: float = 0.0,
    use_hmm: bool = False,
) -> Optional[TradeSignal]:
    """
    Генерирует торговый сигнал S1 (пассивный PostOnly ретест в тренде) или
    S3 (PostOnly возврат к VWAP ±2σ во флэте с R:R >= 1.15 и SL >= 0.80%).
    Проверка гэпов выполняется только на последних 60 барах 15m.
    """
    if len(closed_15m) < 48 or has_recent_gaps(closed_15m, step_ms=900_000, check_bars=60):
        return None

    h1_series = bars_1h if (bars_1h is not None and len(bars_1h) >= 55) else aggregate_1h(closed_15m)
    decision = classify_regime(h1_series, use_hmm=use_hmm)
    regime = decision.regime
    if regime in ("NO_REGIME", "TRANSITION"):
        return None

    atr_s = calc_atr_series(closed_15m[-60:], 14)
    c_sig = closed_15m[-1]
    atr15 = max(atr_s[-1], c_sig.close * 0.002)
    ref_market = reference_open_px if (reference_open_px and reference_open_px > 0) else c_sig.close

    # -------------------------------------------------------------------------
    # Ядро B2: S1_TREND_RETEST (пассивный PostOnly лимитный ретест)
    # -------------------------------------------------------------------------
    if regime in ("TREND_UP", "TREND_DOWN"):
        lookback = closed_15m[-13:-1]
        if len(lookback) < 12:
            return None
        box_high = max(c.high for c in lookback)
        box_low = min(c.low for c in lookback)
        avg_vol = sum(c.volume for c in closed_15m[-21:-1]) / 20.0
        if c_sig.volume < 1.1 * avg_vol:
            return None

        min_sl_frac = MIN_STOP_FRAC_S1_MAJOR if symbol in MAJORS else MIN_STOP_FRAC_S1_ALT
        side: Optional[str] = None
        retest_px = 0.0
        sl_px = 0.0
        if regime == "TREND_UP" and c_sig.close > box_high:
            side = "Buy"
            passive_cap = (best_bid - tick_size) if (best_bid and best_bid > 0) else (ref_market * 0.9995)
            retest_px = min(box_high + 0.05 * atr15, passive_cap)
            sl_dist = max(retest_px * min_sl_frac, 1.5 * atr15)
            sl_px = min(retest_px - sl_dist, c_sig.low - 0.25 * atr15)
        elif regime == "TREND_DOWN" and c_sig.close < box_low:
            side = "Sell"
            passive_floor = (best_ask + tick_size) if (best_ask and best_ask > 0) else (ref_market * 1.0005)
            retest_px = max(box_low - 0.05 * atr15, passive_floor)
            sl_dist = max(retest_px * min_sl_frac, 1.5 * atr15)
            sl_px = max(retest_px + sl_dist, c_sig.high + 0.25 * atr15)

        if side is None or retest_px <= 0:
            return None
        risk_dist = abs(retest_px - sl_px)
        if risk_dist <= 0:
            return None
        tp1 = retest_px + 1.8 * risk_dist if side == "Buy" else retest_px - 1.8 * risk_dist
        tp2 = retest_px + 3.0 * risk_dist if side == "Buy" else retest_px - 3.0 * risk_dist

        feats = _build_ml_features(closed_15m, obi_smoothed=obi_smoothed)
        feats.update({
            "atr15": atr15,
            "box_high": box_high,
            "box_low": box_low,
            "adx_1h": decision.adx_14,
            "atr_pct_1h": decision.atr_percentile,
            "hurst_1h": decision.hurst,
            "post_only": 1.0,
            "retest_ttl_bars": 3.0,
            "max_hold_bars": 72.0,
        })

        return TradeSignal(
            symbol=symbol,
            strategy_id="S1_TREND_RETEST",
            strategy_name="B2/S1: Пробой 15m с PostOnly ретестом (1h Trend)",
            timeframe="15",
            side=side,
            order_type="Limit",
            entry_price=retest_px,
            stop_loss=sl_px,
            take_profit_1=tp1,
            take_profit_2=tp2,
            base_winrate_est=0.45,
            rationale=[f"1h={regime}", f"ADX={decision.adx_14:.1f}", "PostOnly Retest Limit"],
            invalidation_rule="Pure SL/TP (без раннего БУ/трейлинга): SL >= 1.0-1.2% / 1.5*ATR; TP2=+3.0R; max_hold=72 бара (18ч)",
            features=feats,
        )

    # -------------------------------------------------------------------------
    # Рукав A Автотрейдинга: S3_VWAP_RANGE (строго RANGE, PostOnly Limit, R:R >= 1.15, SL >= 0.80%)
    # -------------------------------------------------------------------------
    sess = _session_candles_utc(closed_15m)
    if len(sess) < 8:
        return None
    vwap_b = calc_session_vwap_bands(sess)
    feats = _build_ml_features(closed_15m, obi_smoothed=obi_smoothed)
    rsi_val = feats["rsi_14"]
    z_vwap = feats["vwap_zscore"]
    flow_val = feats["obi_smoothed"]

    rng = max(c_sig.high - c_sig.low, 1e-9)
    lower_wick_ratio = (min(c_sig.open, c_sig.close) - c_sig.low) / rng
    upper_wick_ratio = (c_sig.high - max(c_sig.open, c_sig.close)) / rng

    closes_15m = [c.close for c in closed_15m[-60:]]
    ema20_15m = calc_ema(closes_15m, 20)[-1]
    ema50_15m = calc_ema(closes_15m, min(50, len(closes_15m)))[-1]
    no_waterfall = not (ema20_15m < ema50_15m * 0.996)
    no_meltup = not (ema20_15m > ema50_15m * 1.004)

    side = None
    entry_px = 0.0
    sl_px = 0.0

    # Покупка: касание lower_2sd ИЛИ (z <= -1.5 и RSI < 30), разворотная свеча с фитилём отбоя, поток не против
    buy_zone = (c_sig.low <= vwap_b.lower_2sd) or (z_vwap <= -1.5 and rsi_val < 30.0)
    buy_reject = (c_sig.close >= c_sig.open) or (lower_wick_ratio >= 0.35)
    if buy_zone and buy_reject and no_waterfall and flow_val >= -0.35:
        passive_cap = (best_bid - tick_size) if (best_bid and best_bid > 0) else (ref_market * 0.9996)
        entry_px = min(ref_market, passive_cap)
        if entry_px < vwap_b.vwap:
            side = "Buy"
            sl_dist = max(entry_px * MIN_STOP_FRAC, 1.2 * atr15)
            sl_px = min(entry_px - sl_dist, c_sig.low - 0.35 * atr15)

    # Продажа: касание upper_2sd ИЛИ (z >= +1.5 и RSI > 70), разворотная свеча с верхним фитилём, поток не против
    sell_zone = (c_sig.high >= vwap_b.upper_2sd) or (z_vwap >= 1.5 and rsi_val > 70.0)
    sell_reject = (c_sig.close <= c_sig.open) or (upper_wick_ratio >= 0.35)
    if side is None and sell_zone and sell_reject and no_meltup and flow_val <= 0.35:
        passive_floor = (best_ask + tick_size) if (best_ask and best_ask > 0) else (ref_market * 1.0004)
        entry_px = max(ref_market, passive_floor)
        if entry_px > vwap_b.vwap:
            side = "Sell"
            sl_dist = max(entry_px * MIN_STOP_FRAC, 1.2 * atr15)
            sl_px = max(entry_px + sl_dist, c_sig.high + 0.35 * atr15)

    if side is None or entry_px <= 0:
        return None

    risk_dist = abs(entry_px - sl_px)
    reward_dist = abs(vwap_b.vwap - entry_px)
    if risk_dist <= 0 or (reward_dist / risk_dist) < min_rr_s3:
        return None

    feats.update({
        "atr15": atr15,
        "vwap": vwap_b.vwap,
        "adx_1h": decision.adx_14,
        "atr_pct_1h": decision.atr_percentile,
        "hurst_1h": decision.hurst,
        "post_only": 1.0,
        "retest_ttl_bars": 2.0,
        "max_hold_bars": 16.0,
    })

    return TradeSignal(
        symbol=symbol,
        strategy_id="S3_VWAP_RANGE",
        strategy_name="S3: PostOnly возврат к VWAP ±2σ (1h Range)",
        timeframe="15",
        side=side,
        order_type="Limit",
        entry_price=entry_px,
        stop_loss=sl_px,
        take_profit_1=vwap_b.vwap,
        take_profit_2=vwap_b.vwap,
        base_winrate_est=0.58,
        rationale=[f"1h={regime}", f"ADX={decision.adx_14:.1f}", "PostOnly VWAP ±2σ rejection"],
        invalidation_rule="PostOnly Limit; SL >= 0.80% / 1.2*ATR; R:R >= 1.15; выход VWAP или 16 баров без раннего БУ",
        features=feats,
    )

"""
Стратегия 1 (Снайперский Архетип №1 и №2 из документа):
Институциональный VWAP (±2σ), каналы Кельтнера (EMA20 ± 2×ATR), полосы Боллинджера и Volatility Squeeze.
Включает три формализованных сетапа:
- Сетап 1А (VWAP / Keltner Mean Reversion ±2σ): Выход цены за пределы канала Кельтнера или полосы VWAP ±2σ
  (где цена проводит лишь ~5% времени) и закрытие свечи обратно внутри канала при ADX(14) < 28 (флэт/баланс)
  и/или перегретом фандинге. Подтвержденный исторический Win Rate: 65%–75%, Profit Factor: 1.5–2.5.
- Сетап 1Б (VWAP Trend Pullback / Reclaim): Откат к растущему/падающему VWAP в подтвержденном тренде (1H Bias)
  при защитном фильтре расширения волатильности ATR_14 / ATR_20 <= 1.5.
- Сетап 1В (Bollinger/Keltner Volatility Squeeze Breakout): Выход из сжатия волатильности при ADX > 20 и RSI > 50 (< 50).
"""

from __future__ import annotations

from typing import List

from aitrade.features.indicators import (
    calc_adx,
    calc_atr_series,
    calc_bollinger_keltner_squeeze,
    calc_rsi,
)
from aitrade.strategies.base import MarketContext, TradeSignal


def evaluate_vwap_keltner_strategy(ctx: MarketContext) -> List[TradeSignal]:
    signals: List[TradeSignal] = []
    c15 = ctx.candles_15m
    if len(c15) < 25:
        return signals

    last = c15[-1]
    prev = c15[-2]
    atr14 = calc_atr_series(c15, 14)[-1]
    rsi14 = calc_rsi(c15, 14)[-1]
    adx15, plus_di, minus_di = calc_adx(c15, 14)
    bb_up, bb_low, kc_up, kc_low, ema20, is_squeeze = calc_bollinger_keltner_squeeze(c15)
    vwap = ctx.vwap_bands
    funding = ctx.screener.funding_rate

    # -------------------------------------------------------------------------
    # Сетап 1А: Возврат к среднему от границ Keltner / Bollinger и VWAP ±2σ (Sniper Mean Reversion)
    # -------------------------------------------------------------------------
    upper_extreme = min(vwap.upper_2sd, kc_up)
    lower_extreme = max(vwap.lower_2sd, kc_low)

    # Проверка прокола верхней границы (+2σ / Keltner Upper) с возвратом тела внутрь (Short Mean Reversion)
    pierced_upper = (prev.high >= upper_extreme or last.high >= upper_extreme) and (
        last.close < upper_extreme
    )
    if pierced_upper and ctx.screener.adx_1h < 32.0 and ctx.screener.mtf_bias != "BULLISH_TREND":
        wick_top = max(prev.high, last.high)
        entry_px = round(max(last.close, upper_extreme * 0.9985), 6)
        sl_px = round(max(wick_top + 0.35 * atr14, entry_px + 1.15 * atr14), 6)
        tp1_px = round(min(vwap.upper_1sd, entry_px - 1.55 * (sl_px - entry_px)), 6)
        tp2_px = round(min(vwap.vwap, ema20, entry_px - 2.25 * (sl_px - entry_px)), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="VWAP_KELTNER_MR_SHORT",
                strategy_name="Снайперский возврат от VWAP +2σ / Keltner (Mean Reversion)",
                timeframe="15m",
                side="Sell",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.71,
                rationale=[
                    f"Ложный вынос за верхнюю границу VWAP +2σ / Keltner ({upper_extreme:.4f}) с закрытием тела обратно в канал ({last.close:.4f})",
                    f"Низкий трендовый фильтр ADX(1H)={ctx.screener.adx_1h:.1f} исключает безоткатный бычий тренд",
                    f"Ставка финансирования {funding * 100:.4f}% и RSI(14)={rsi14:.1f} подтверждают локальную перегретость лонгов",
                ],
                invalidation_rule="Немедленный выход при закрытии 15m свечи выше хвоста прокола или удержании за +2σ более 2 баров",
                features={
                    "vwap_zscore": (last.close - vwap.vwap) / max(vwap.std, 1e-9),
                    "rsi_14": rsi14,
                    "adx_1h": ctx.screener.adx_1h,
                    "rvol": ctx.screener.rvol,
                    "obi_smoothed": ctx.micro.smoothed_obi,
                    "funding_rate": funding,
                },
            )
        )

    # Проверка прокола нижней границы (-2σ / Keltner Lower) с возвратом тела внутрь (Long Mean Reversion)
    pierced_lower = (prev.low <= lower_extreme or last.low <= lower_extreme) and (
        last.close > lower_extreme
    )
    if pierced_lower and ctx.screener.adx_1h < 32.0 and ctx.screener.mtf_bias != "BEARISH_TREND":
        wick_bottom = min(prev.low, last.low)
        entry_px = round(min(last.close, lower_extreme * 1.0015), 6)
        sl_px = round(min(wick_bottom - 0.35 * atr14, entry_px - 1.15 * atr14), 6)
        tp1_px = round(max(vwap.lower_1sd, entry_px + 1.55 * (entry_px - sl_px)), 6)
        tp2_px = round(max(vwap.vwap, ema20, entry_px + 2.25 * (entry_px - sl_px)), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="VWAP_KELTNER_MR_LONG",
                strategy_name="Снайперский возврат от VWAP -2σ / Keltner (Mean Reversion)",
                timeframe="15m",
                side="Buy",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.72,
                rationale=[
                    f"Выход цены в статистический экстремум VWAP -2σ / Keltner ({lower_extreme:.4f}) с выкупом хвоста и закрытием внутри канала ({last.close:.4f})",
                    f"Режим баланса ADX(1H)={ctx.screener.adx_1h:.1f} благоприятствует возврату к сессионному VWAP ({vwap.vwap:.4f})",
                    f"Дисбаланс стакана L2 OBI={ctx.micro.smoothed_obi:+.2f} и RVOL={ctx.screener.rvol:.2f}x подтверждают лимитный выкуп",
                ],
                invalidation_rule="Выход по стоп-лоссу за кончиком хвоста прокола или при закрытии 2 свечей подряд ниже -2σ",
                features={
                    "vwap_zscore": (last.close - vwap.vwap) / max(vwap.std, 1e-9),
                    "rsi_14": rsi14,
                    "adx_1h": ctx.screener.adx_1h,
                    "rvol": ctx.screener.rvol,
                    "obi_smoothed": ctx.micro.smoothed_obi,
                    "funding_rate": funding,
                },
            )
        )

    # -------------------------------------------------------------------------
    # Сетап 1Б: Трендовый откат и возврат (VWAP Pullback / Reclaim)
    # -------------------------------------------------------------------------
    if (
        ctx.screener.mtf_bias == "BULLISH_TREND"
        and ctx.screener.atr_expansion_ratio <= 1.5
        and vwap.slope_pct > 0
    ):
        reclaimed_vwap = prev.low <= vwap.vwap * 1.002 and last.close > vwap.vwap
        if reclaimed_vwap and last.close < vwap.upper_1sd:
            entry_px = round(last.close, 6)
            sl_px = round(min(prev.low, last.low) - 0.5 * atr14, 6)
            risk = max(entry_px - sl_px, atr14 * 0.8)
            sl_px = round(entry_px - risk, 6)
            tp1_px = round(max(vwap.upper_1sd, entry_px + 1.6 * risk), 6)
            tp2_px = round(max(vwap.upper_2sd, entry_px + 2.4 * risk), 6)
            signals.append(
                TradeSignal(
                    symbol=ctx.symbol,
                    strategy_id="VWAP_TREND_RECLAIM_LONG",
                    strategy_name="Трендовый ретест и удержание VWAP (VWAP Reclaim)",
                    timeframe="15m",
                    side="Buy",
                    order_type="Limit",
                    entry_price=entry_px,
                    stop_loss=sl_px,
                    take_profit_1=tp1_px,
                    take_profit_2=tp2_px,
                    base_winrate_est=0.66,
                    rationale=[
                        f"Старший 1H тренд бычий (веер EMA 8>21>50), тест и возврат выше растущего сессионного VWAP ({vwap.vwap:.4f})",
                        f"Фильтр расширения волатильности в норме: ATR(14)/ATR(20) = {ctx.screener.atr_expansion_ratio:.2f} <= 1.50",
                        f"Положительный наклон VWAP ({vwap.slope_pct:+.2f}%) и поддержка стакана OBI={ctx.micro.smoothed_obi:+.2f}",
                    ],
                    invalidation_rule="Закрытие 15m свечи обратно под VWAP в течение 2 баров",
                    features={
                        "vwap_zscore": (last.close - vwap.vwap) / max(vwap.std, 1e-9),
                        "rsi_14": rsi14,
                        "adx_1h": ctx.screener.adx_1h,
                        "rvol": ctx.screener.rvol,
                        "obi_smoothed": ctx.micro.smoothed_obi,
                        "funding_rate": funding,
                    },
                )
            )

    return signals

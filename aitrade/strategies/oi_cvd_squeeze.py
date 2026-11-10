"""
Стратегия 3 (Снайперский Архетип №3 / Раздел 3.1 документа):
Торговля на каскадах принудительных ликвидаций, дельте Открытого Интереса (OI),
токсичном потоке VPIN (TOF) и кумулятивной дельте объема (CVD).
Реализует матрицу 4 рыночных сценариев из документа:
1. Цена UP + OI падает (-) + истощение CVD -> Short Squeeze Exhaustion (контртрендовый Шорт после выноса шортов).
2. Цена DOWN + OI падает (-) + спайк VPIN/TOF -> Long Flush Exhaustion (набор Лонга на возврат к среднему после каскадной ликвидации лонгов).
3. Цена UP + OI растет (+) + положительная CVD -> Подтвержденный приток капитала (Лонг на откате).
"""

from __future__ import annotations

from typing import List

from aitrade.features.indicators import calc_atr_series, calc_rsi
from aitrade.strategies.base import MarketContext, TradeSignal


def evaluate_oi_cvd_squeeze_strategy(ctx: MarketContext) -> List[TradeSignal]:
    signals: List[TradeSignal] = []
    c15 = ctx.candles_15m
    oi_hist = ctx.oi_history
    if len(c15) < 15 or len(oi_hist) < 5:
        return signals

    last = c15[-1]
    atr14 = calc_atr_series(c15, 14)[-1]
    rsi14 = calc_rsi(c15, 14)[-1]

    # Изменение цены и открытого интереса за последние 3 бара (45 минут)
    price_change_pct = ((last.close - c15[-4].close) / max(c15[-4].close, 1e-9)) * 100.0
    oi_now = oi_hist[-1][1]
    oi_prev = oi_hist[-4][1]
    oi_delta_pct = ((oi_now - oi_prev) / max(oi_prev, 1e-9)) * 100.0

    # -------------------------------------------------------------------------
    # Сценарий А: Каскадная ликвидация лонгов (Long Flush) -> Цена упала, OI резко упал (-1.5%..), VPIN высокий
    # -------------------------------------------------------------------------
    if (
        price_change_pct <= -0.65
        and oi_delta_pct <= -1.2
        and last.close > last.low + 0.35 * (last.high - last.low)
    ):
        entry_px = round(last.close, 6)
        sl_px = round(min(last.low, c15[-2].low) - 0.6 * atr14, 6)
        risk = max(entry_px - sl_px, atr14 * 0.8)
        sl_px = round(entry_px - risk, 6)
        tp1_px = round(max(ctx.vwap_bands.lower_1sd, entry_px + 1.7 * risk), 6)
        tp2_px = round(max(ctx.vwap_bands.vwap, ctx.volume_profile.poc, entry_px + 2.5 * risk), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="OI_LONG_FLUSH_REVERSAL",
                strategy_name="Разворот после каскадной ликвидации лонгов (Long Flush + OI Drop)",
                timeframe="15m",
                side="Buy",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.69,
                rationale=[
                    f"Сброс открытого интереса OI на {oi_delta_pct:.2f}% при падении цены на {price_change_pct:.2f}% подтверждает принудительное закрытие маржинальных лонгов (Long Flush)",
                    f"Индикатор токсичного потока VPIN (TOF)={ctx.micro.vpin_tof:.2f} зафиксировал кульминацию рыночных продаж и переход к лимитному выкупу",
                    f"Цель возврата — справедливая цена сессии VWAP ({ctx.vwap_bands.vwap:.4f}) и уровень максимального объема POC ({ctx.volume_profile.poc:.4f})",
                ],
                invalidation_rule="Обновление минимума каскадной свечи при растущем открытом интересе",
                features={
                    "vwap_zscore": (last.close - ctx.vwap_bands.vwap) / max(ctx.vwap_bands.std, 1e-9),
                    "rsi_14": rsi14,
                    "adx_1h": ctx.screener.adx_1h,
                    "rvol": ctx.screener.rvol,
                    "obi_smoothed": ctx.micro.smoothed_obi,
                    "funding_rate": ctx.screener.funding_rate,
                },
            )
        )

    # -------------------------------------------------------------------------
    # Сценарий Б: Истощение шорт-сквиза (Short Squeeze Exhaustion) -> Цена выросла к VAH/+1..2σ, но OI падает
    # -------------------------------------------------------------------------
    if (
        price_change_pct >= 0.65
        and oi_delta_pct <= -1.2
        and last.close >= ctx.vwap_bands.upper_1sd
    ):
        entry_px = round(last.close, 6)
        sl_px = round(max(last.high, c15[-2].high) + 0.6 * atr14, 6)
        risk = max(sl_px - entry_px, atr14 * 0.8)
        sl_px = round(entry_px + risk, 6)
        tp1_px = round(min(ctx.vwap_bands.upper_1sd, entry_px - 1.7 * risk), 6)
        tp2_px = round(min(ctx.vwap_bands.vwap, ctx.volume_profile.poc, entry_px - 2.5 * risk), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="OI_SHORT_SQUEEZE_EXHAUSTION",
                strategy_name="Истощение шорт-сквиза на падающем Открытом Интересе (OI Divergence)",
                timeframe="15m",
                side="Sell",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.68,
                rationale=[
                    f"Импульс вверх (+{price_change_pct:.2f}%) сопровождается падением OI ({oi_delta_pct:.2f}%): движение вызвано закрытием шортов без притока новых покупателей",
                    f"Цена достигла верхней зоны стоимости VAH ({ctx.volume_profile.vah:.4f}) при VPIN={ctx.micro.vpin_tof:.2f}",
                    f"Ожидается возврат к сессионному VWAP ({ctx.vwap_bands.vwap:.4f}) после завершения выноса",
                ],
                invalidation_rule="Пробой локального максимума сквиза с разворотом дельты OI в плюс",
                features={
                    "vwap_zscore": (last.close - ctx.vwap_bands.vwap) / max(ctx.vwap_bands.std, 1e-9),
                    "rsi_14": rsi14,
                    "adx_1h": ctx.screener.adx_1h,
                    "rvol": ctx.screener.rvol,
                    "obi_smoothed": ctx.micro.smoothed_obi,
                    "funding_rate": ctx.screener.funding_rate,
                },
            )
        )

    return signals

"""
Стратегия 2 (Снайперский Архетип №2 / Раздел 3.4 документа):
Снятие ликвидности (Liquidity Sweep / SFP за 1–3 свечи) + Импульсный дисбаланс (Fair Value Gap — FVG)
+ Подтверждение по дельте объема (CVD Divergence & Absorption).
Подтвержденный исторический Win Rate: ~65%–68%, Profit Factor: ~1.7+, R:R от 1:2 до 1:5.
"""

from __future__ import annotations

from typing import List

from aitrade.features.indicators import calc_atr_series, calc_rsi
from aitrade.features.microstructure import calc_candle_cvd_series
from aitrade.strategies.base import MarketContext, TradeSignal


def evaluate_liquidity_sweep_fvg_strategy(ctx: MarketContext) -> List[TradeSignal]:
    signals: List[TradeSignal] = []
    c15 = ctx.candles_15m
    if len(c15) < 30:
        return signals

    atr14 = calc_atr_series(c15, 14)[-1]
    rsi14 = calc_rsi(c15, 14)[-1]
    cvd = calc_candle_cvd_series(c15)

    # Определяем пулы ликвидности BSL (Buy-Side Liquidity) и SSL (Sell-Side Liquidity)
    # на предшествующем окне (свечи от -28 до -4)
    prior_window = c15[-28:-3]
    ssl_level = min(c.low for c in prior_window)
    bsl_level = max(c.high for c in prior_window)
    prior_min_idx = min(range(len(prior_window)), key=lambda i: prior_window[i].low)
    prior_max_idx = max(range(len(prior_window)), key=lambda i: prior_window[i].high)
    cvd_at_ssl = cvd[len(c15) - 28 + prior_min_idx]
    cvd_at_bsl = cvd[len(c15) - 28 + prior_max_idx]

    c_m3, c_m2, c_m1 = c15[-3], c15[-2], c15[-1]

    # -------------------------------------------------------------------------
    # Бычий сетап: Снятие Sell-Side Liquidity (SSL) за 1-3 свечи + Бычий FVG + CVD Absorption
    # -------------------------------------------------------------------------
    swept_ssl = min(c_m3.low, c_m2.low) < ssl_level and c_m1.close > ssl_level
    # Трехсвечной бычий FVG: зазор между high первой свечи (c_m3) и low третьей (c_m1), либо импульсное тело c_m2
    fvg_bottom = min(c_m3.high, c_m2.open)
    fvg_top = max(c_m1.low, c_m2.close)
    has_bullish_displacement = (c_m2.close > c_m2.open) and (
        (c_m2.close - c_m2.open) >= 0.45 * atr14 or c_m1.low > c_m3.high * 0.998
    )
    # Дивергенция / поглощение CVD: цена обновила минимум ниже ssl_level, но CVD выше или стабильна
    cvd_bullish_div = (cvd[-2] >= cvd_at_ssl * 0.95) or (ctx.micro.smoothed_obi > 0.08)

    if swept_ssl and has_bullish_displacement and cvd_bullish_div:
        sweep_wick_low = min(c_m3.low, c_m2.low, c_m1.low)
        # Лимитный вход от 50% середины FVG (Consequent Encroachment) или текущей цены ретеста
        ce_midpoint = 0.5 * (fvg_bottom + fvg_top)
        entry_px = round(min(c_m1.close, max(ce_midpoint, ssl_level)), 6)
        sl_px = round(min(sweep_wick_low - 0.25 * atr14, entry_px - 0.9 * atr14), 6)
        risk = max(entry_px - sl_px, 1e-9)
        tp1_px = round(max(ctx.volume_profile.poc, ctx.vwap_bands.vwap, entry_px + 2.0 * risk), 6)
        tp2_px = round(max(bsl_level, ctx.volume_profile.vah, entry_px + 3.0 * risk), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="SMC_LIQ_SWEEP_FVG_LONG",
                strategy_name="Ложный пробой SSL (Liquidity Sweep) + лимит в 50% FVG",
                timeframe="15m",
                side="Buy",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.67,
                rationale=[
                    f"Снятие пула стоп-ордеров SSL под уровнем {ssl_level:.4f} (прокол до {sweep_wick_low:.4f}) с быстрым возвратом тела за 1–2 свечи",
                    f"Импульсное смещение (Displacement) сформировало бычий 15m FVG со средней линией CE {ce_midpoint:.4f}",
                    f"Поглощение продаж по дельте (CVD Divergence) и перевес лимитных бидов в стакане OBI={ctx.micro.smoothed_obi:+.2f}",
                ],
                invalidation_rule="Закрытие 15m свечи ниже кончика хвоста снятия ликвидности (Sweep Wick)",
                features={
                    "vwap_zscore": (c_m1.close - ctx.vwap_bands.vwap) / max(ctx.vwap_bands.std, 1e-9),
                    "rsi_14": rsi14,
                    "adx_1h": ctx.screener.adx_1h,
                    "rvol": ctx.screener.rvol,
                    "obi_smoothed": ctx.micro.smoothed_obi,
                    "funding_rate": ctx.screener.funding_rate,
                },
            )
        )

    # -------------------------------------------------------------------------
    # Медвежий сетап: Снятие Buy-Side Liquidity (BSL) за 1-3 свечи + Медвежий FVG + CVD Absorption
    # -------------------------------------------------------------------------
    swept_bsl = max(c_m3.high, c_m2.high) > bsl_level and c_m1.close < bsl_level
    has_bearish_displacement = (c_m2.close < c_m2.open) and (
        (c_m2.open - c_m2.close) >= 0.45 * atr14 or c_m1.high < c_m3.low * 1.002
    )
    cvd_bearish_div = (cvd[-2] <= cvd_at_bsl * 1.05) or (ctx.micro.smoothed_obi < -0.08)

    if swept_bsl and has_bearish_displacement and cvd_bearish_div:
        sweep_wick_high = max(c_m3.high, c_m2.high, c_m1.high)
        ce_midpoint = 0.5 * (c_m2.open + c_m2.close)
        entry_px = round(max(c_m1.close, min(ce_midpoint, bsl_level)), 6)
        sl_px = round(max(sweep_wick_high + 0.25 * atr14, entry_px + 0.9 * atr14), 6)
        risk = max(sl_px - entry_px, 1e-9)
        tp1_px = round(min(ctx.volume_profile.poc, ctx.vwap_bands.vwap, entry_px - 2.0 * risk), 6)
        tp2_px = round(min(ssl_level, ctx.volume_profile.val, entry_px - 3.0 * risk), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="SMC_LIQ_SWEEP_FVG_SHORT",
                strategy_name="Ложный пробой BSL (Liquidity Sweep) + лимит в 50% FVG",
                timeframe="15m",
                side="Sell",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.66,
                rationale=[
                    f"Снятие стопов покупателей (BSL) над максимумом {bsl_level:.4f} (шпилька до {sweep_wick_high:.4f}) с закрытием обратно в диапазон",
                    f"Медвежий импульс оставил зону имбаланса FVG (50% ретест на {ce_midpoint:.4f})",
                    f"Лимитное поглощение покупок сверху (OBI={ctx.micro.smoothed_obi:+.2f}) на фоне RVOL={ctx.screener.rvol:.2f}x",
                ],
                invalidation_rule="Закрытие 15m свечи выше кончика хвоста снятия ликвидности",
                features={
                    "vwap_zscore": (c_m1.close - ctx.vwap_bands.vwap) / max(ctx.vwap_bands.std, 1e-9),
                    "rsi_14": rsi14,
                    "adx_1h": ctx.screener.adx_1h,
                    "rvol": ctx.screener.rvol,
                    "obi_smoothed": ctx.micro.smoothed_obi,
                    "funding_rate": ctx.screener.funding_rate,
                },
            )
        )

    return signals

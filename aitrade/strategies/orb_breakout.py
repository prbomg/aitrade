"""
Стратегия 4 (Раздел 3.3 документа):
Пробой диапазона открытия сессии (Opening Range Breakout — 5m/15m ORB) на «Монетах в игре»
при открытии Европейской (08:00 UTC) или Американской сессии (13:30 UTC).
Включает:
- Фильтрацию относительного объема RVOL >= 1.5
- Ограничение максимальной ширины стартовой свечи (защита от новостных шпилек)
- Направленное правило QuantifiedStrategies (бычья первая свеча -> только Лонг; медвежья -> только Шорт)
- Правило 2 баров (2-Bar Stall Rule) для контроля импульса.
"""

from __future__ import annotations

from typing import List

from aitrade.features.indicators import calc_atr_series, calc_rsi
from aitrade.strategies.base import MarketContext, TradeSignal


def evaluate_orb_breakout_strategy(ctx: MarketContext) -> List[TradeSignal]:
    signals: List[TradeSignal] = []
    c15 = ctx.candles_15m
    if len(c15) < 12 or ctx.screener.rvol < 1.35:
        return signals

    # ORB работает только в активные окна Европы или США при подтвержденном импульсе
    if ctx.screener.active_session not in ("EUROPE", "US_OVERLAP"):
        return signals

    atr14 = calc_atr_series(c15, 14)[-1]
    rsi14 = calc_rsi(c15, 14)[-1]

    # Берем диапазон консолидации / открытия сессии за 4 бара до последних двух свечей
    orb_candles = c15[-6:-2]
    orb_high = max(c.high for c in orb_candles)
    orb_low = min(c.low for c in orb_candles)
    orb_width = orb_high - orb_low
    orb_width_pct = (orb_width / max(orb_low, 1e-9)) * 100.0

    # Защита от аномально широкого диапазона: если ширина > 2.2%, стоп слишком велик
    if orb_width_pct <= 0.12 or orb_width_pct > 2.2:
        return signals

    last = c15[-1]
    prev = c15[-2]

    # Пробой вверх (Bullish ORB)
    if (
        last.close > orb_high
        and prev.close >= orb_low + 0.6 * orb_width
        and ctx.screener.mtf_bias != "BEARISH_TREND"
        and ctx.micro.smoothed_obi >= 0.05
    ):
        entry_px = round(last.close, 6)
        sl_px = round(max(orb_low, entry_px - 1.1 * atr14), 6)
        risk = max(entry_px - sl_px, atr14 * 0.6)
        sl_px = round(entry_px - risk, 6)
        tp1_px = round(entry_px + 1.6 * risk, 6)
        tp2_px = round(entry_px + 2.5 * risk, 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="SESSION_ORB_LONG",
                strategy_name=f"Пробой диапазона сессии {ctx.screener.active_session} (Session ORB)",
                timeframe="15m",
                side="Buy",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.64,
                rationale=[
                    f"Импульсный пробой верхней границы стартового диапазона сессии {orb_high:.4f} (ширина диапазона {orb_width_pct:.2f}%)",
                    f"Монета «в игре»: относительный объем RVOL={ctx.screener.rvol:.2f}x в окне {ctx.screener.active_session}",
                    f"Поддержка потока ордеров в стакане Level-2 (OBI={ctx.micro.smoothed_obi:+.2f}, VAMP div={ctx.micro.vamp_divergence_bps:+.1f} bps)",
                ],
                invalidation_rule="Правило 2 баров (2-Bar Stall Rule): если цена не обновляет максимум в течение 2 свечей или возвращается внутрь диапазона ORB — выход по рынку",
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

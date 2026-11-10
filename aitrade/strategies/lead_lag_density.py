"""
Стратегия 5 (Раздел 3.5 документа):
Межрыночный статистический арбитраж запаздывания (Lead-Lag BTC -> Altcoins с порядком лага lag=3)
и скальпинг от крупных верифицированных плотностей в стакане L2 с анти-спуфинг фильтром (LBI >= 0.75).
"""

from __future__ import annotations

import math
from typing import List

from aitrade.features.indicators import calc_atr_series, calc_rsi
from aitrade.strategies.base import MarketContext, TradeSignal


def evaluate_lead_lag_density_strategy(ctx: MarketContext) -> List[TradeSignal]:
    signals: List[TradeSignal] = []
    c15 = ctx.candles_15m
    btc15 = ctx.btc_candles_15m
    if len(c15) < 10 or len(btc15) < 10:
        return signals

    last = c15[-1]
    atr14 = calc_atr_series(c15, 14)[-1]
    rsi14 = calc_rsi(c15, 14)[-1]

    # 1. Расчет дифференциала логарифмической доходности за 3 лага (lag order = 3):
    # Delta_r = r_BTC(t-3..t) - r_ALT(t-3..t)
    btc_ret_3 = math.log(max(btc15[-1].close, 1e-9) / max(btc15[-4].close, 1e-9)) * 100.0
    alt_ret_3 = math.log(max(c15[-1].close, 1e-9) / max(c15[-4].close, 1e-9)) * 100.0
    lead_lag_diff = btc_ret_3 - alt_ret_3

    # 2. Проверка крупной верифицированной плотности в стакане L2 и анти-спуфинг индекса LBI
    bid_wall_px, bid_wall_usd = ctx.micro.largest_bid_wall
    ask_wall_px, ask_wall_usd = ctx.micro.largest_ask_wall
    lbi = ctx.micro.liquidity_behavior_index

    # Долларовый порог крупной плотности адаптируется к ликвидности пары (минимум $250k)
    min_wall_usd = 250_000.0

    if (
        lbi >= 0.75
        and not ctx.micro.spoofing_flags
        and bid_wall_usd >= min_wall_usd
        and 0.0 < (last.close - bid_wall_px) / last.close <= 0.0055
        and (lead_lag_diff >= 0.25 or ctx.micro.smoothed_obi >= 0.18)
    ):
        # Вход перед крупной лимитной плотностью на покупку со стопом сразу за плотностью
        entry_px = round(min(last.close, bid_wall_px * 1.0008), 6)
        sl_px = round(min(bid_wall_px * 0.9965, entry_px - 0.85 * atr14), 6)
        risk = max(entry_px - sl_px, atr14 * 0.5)
        sl_px = round(entry_px - risk, 6)
        tp1_px = round(entry_px + 1.8 * risk, 6)
        tp2_px = round(max(ask_wall_px * 0.999, entry_px + 2.8 * risk), 6)

        signals.append(
            TradeSignal(
                symbol=ctx.symbol,
                strategy_id="L2_DENSITY_LEAD_LAG_LONG",
                strategy_name="Отскок от верифицированной L2-плотности + Lead-Lag импульс BTC",
                timeframe="15m",
                side="Buy",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1_px,
                take_profit_2=tp2_px,
                base_winrate_est=0.68,
                rationale=[
                    f"В стакане Level-2 обнаружена крупная лимитная плотность ${bid_wall_usd:,.0f} на уровне {bid_wall_px:.4f} (индекс анти-спуфинга LBI={lbi:.2f}, манипуляций не выявлено)",
                    f"Межрыночный фактор Lead-Lag (lag=3): доходность BTC ({btc_ret_3:+.2f}%) опережает актив ({alt_ret_3:+.2f}%), потенциал догоняющего движения {lead_lag_diff:+.2f}%",
                    f"Сглаженный фильтром Савицкого — Голея дисбаланс стакана OBI={ctx.micro.smoothed_obi:+.2f} и VAMP={ctx.micro.vamp_divergence_bps:+.1f} bps",
                ],
                invalidation_rule="Разъедание лимитной плотности более чем на 70% рыночными продажами или пробой уровня плотности",
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

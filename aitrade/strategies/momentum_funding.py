# -*- coding: utf-8 -*-
"""
Рукав B Модуля «Автотрейдинг» (35% капитала) — Кросс-секционный «Моментум + Фандинг» (1h / 4h):
- Раз в 4 часа ранжирует пул ликвидных пар по скорректированному на волатильность 24ч моментуму
  (ret_24h / natr_1h) с поправкой на ставку финансирования (fundingRate).
- Лонг топ-1..2 сильных монет с дешёвым/отрицательным фандингом.
- Шорт топ-1..2 слабых монет с перегретым положительным фандингом.
- Исполнение PostOnly Limit, SL = max(0.80%, 2.0 * ATR(1h)), ребалансировка / TP по 2.5R или 8h.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.base import TradeSignal


@dataclass
class MomentumFundingRank:
    symbol: str
    ret_24h_pct: float
    natr_1h_pct: float
    vol_adj_momentum: float
    funding_rate: float
    composite_score: float


def rank_momentum_funding(
    candles_1h_by_symbol: Dict[str, List[Candle]],
    funding_rates: Dict[str, float],
) -> List[MomentumFundingRank]:
    """Ранжирует монеты по волатильностно-скорректированному 24ч моментуму и ставке финансирования."""
    raw_rows: List[MomentumFundingRank] = []
    for sym, bars in candles_1h_by_symbol.items():
        if len(bars) < 30:
            continue
        last_close = bars[-1].close
        prev_24h = bars[-25].close
        if last_close <= 0 or prev_24h <= 0:
            continue
        ret_24h_pct = ((last_close - prev_24h) / prev_24h) * 100.0
        atr_1h = calc_atr_series(bars, 14)[-1]
        natr_1h_pct = max(0.25, (atr_1h / last_close) * 100.0)
        vol_adj_mom = ret_24h_pct / natr_1h_pct
        fr = float(funding_rates.get(sym, 0.0) or 0.0)
        # Отрицательный/дешёвый фандинг повышает привлекательность лонга, высокий положительный — шорта
        funding_bps = fr * 10_000.0
        composite = vol_adj_mom - 0.35 * funding_bps
        raw_rows.append(
            MomentumFundingRank(
                symbol=sym,
                ret_24h_pct=round(ret_24h_pct, 3),
                natr_1h_pct=round(natr_1h_pct, 3),
                vol_adj_momentum=round(vol_adj_mom, 3),
                funding_rate=fr,
                composite_score=round(composite, 3),
            )
        )
    raw_rows.sort(key=lambda r: r.composite_score, reverse=True)
    return raw_rows


def generate_momentum_funding_signals(
    candles_1h_by_symbol: Dict[str, List[Candle]],
    funding_rates: Dict[str, float],
    top_n: int = 1,
    min_mom_threshold: float = 0.8,
) -> List[TradeSignal]:
    """
    Генерирует кросс-секционные сигналы «Моментум + Фандинг»:
    - Long: топ-N лидеров (vol_adj_momentum >= min_mom_threshold, funding_rate <= +0.015%)
    - Short: топ-N аутсайдеров (vol_adj_momentum <= -min_mom_threshold, funding_rate >= +0.005%)
    """
    ranked = rank_momentum_funding(candles_1h_by_symbol, funding_rates)
    if len(ranked) < 2:
        return []

    signals: List[TradeSignal] = []

    # Лонги: лидеры моментума с дешёвым/отрицательным фандингом
    longs_added = 0
    for r in ranked:
        if longs_added >= top_n:
            break
        if r.vol_adj_momentum < min_mom_threshold or r.funding_rate > 0.00015:
            continue
        bars = candles_1h_by_symbol[r.symbol]
        entry_px = bars[-1].close * 0.9996
        atr_1h = max(calc_atr_series(bars, 14)[-1], entry_px * 0.004)
        sl_dist = max(entry_px * 0.0080, 2.0 * atr_1h)
        sl_px = entry_px - sl_dist
        tp1 = entry_px + 1.5 * sl_dist
        tp2 = entry_px + 2.5 * sl_dist
        signals.append(
            TradeSignal(
                symbol=r.symbol,
                strategy_id="AUTOTRADE_MOMENTUM_FUNDING",
                strategy_name="Рукав B: Кросс-секционный Моментум + Фандинг (Long)",
                timeframe="60",
                side="Buy",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1,
                take_profit_2=tp2,
                base_winrate_est=0.55,
                rationale=[
                    f"Mom/NATR={r.vol_adj_momentum:+.2f}",
                    f"Funding={r.funding_rate * 100:.4f}%",
                    f"Score={r.composite_score:+.2f}",
                ],
                invalidation_rule="SL = 2.0*ATR(1h); ребалансировка каждые 4-8ч",
                features={
                    "vol_adj_momentum": r.vol_adj_momentum,
                    "funding_rate": r.funding_rate,
                    "composite_score": r.composite_score,
                    "post_only": 1.0,
                    "retest_ttl_bars": 2.0,
                    "max_hold_bars": 8.0,
                },
            )
        )
        longs_added += 1

    # Шорты: аутсайдеры моментума с положительным (перегретым) фандингом
    shorts_added = 0
    for r in reversed(ranked):
        if shorts_added >= top_n:
            break
        if r.vol_adj_momentum > -min_mom_threshold or r.funding_rate < 0.00005:
            continue
        bars = candles_1h_by_symbol[r.symbol]
        entry_px = bars[-1].close * 1.0004
        atr_1h = max(calc_atr_series(bars, 14)[-1], entry_px * 0.004)
        sl_dist = max(entry_px * 0.0080, 2.0 * atr_1h)
        sl_px = entry_px + sl_dist
        tp1 = entry_px - 1.5 * sl_dist
        tp2 = entry_px - 2.5 * sl_dist
        signals.append(
            TradeSignal(
                symbol=r.symbol,
                strategy_id="AUTOTRADE_MOMENTUM_FUNDING",
                strategy_name="Рукав B: Кросс-секционный Моментум + Фандинг (Short)",
                timeframe="60",
                side="Sell",
                order_type="Limit",
                entry_price=entry_px,
                stop_loss=sl_px,
                take_profit_1=tp1,
                take_profit_2=tp2,
                base_winrate_est=0.55,
                rationale=[
                    f"Mom/NATR={r.vol_adj_momentum:+.2f}",
                    f"Funding={r.funding_rate * 100:.4f}%",
                    f"Score={r.composite_score:+.2f}",
                ],
                invalidation_rule="SL = 2.0*ATR(1h); ребалансировка каждые 4-8ч",
                features={
                    "vol_adj_momentum": r.vol_adj_momentum,
                    "funding_rate": r.funding_rate,
                    "composite_score": r.composite_score,
                    "post_only": 1.0,
                    "retest_ttl_bars": 2.0,
                    "max_hold_bars": 8.0,
                },
            )
        )
        shorts_added += 1

    return signals

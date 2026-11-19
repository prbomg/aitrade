# -*- coding: utf-8 -*-
"""
Рукав C Модуля «Автотрейдинг» (25% капитала) — Дельта-нейтральный Funding Carry:
- Вход (Long Spot + Short Linear Perp равного номинала) только при устойчивом
  фандинге >= 0.025% / 8h (0.00025) на протяжении >= 9 интервалов (3 суток).
- Плечо 1x-2x, резерв маржи >= 30%, контроль окупаемости комиссий.
- Выход при падении среднего фандинга за последние 3 интервала ниже 0.005% / 8h
  или при переходе текущей ставки в отрицательную зону.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CarryOpportunity:
    symbol: str
    action: str  # OPEN_CARRY | HOLD | CLOSE_CARRY | SKIP
    avg_funding_9_8h: float
    avg_funding_3_8h: float
    annualized_apr_pct: float
    basis_pct: float
    payback_intervals: float
    recommended_leverage: float
    margin_reserve_pct: float
    reason: str


class FundingCarryEngine:
    MIN_ENTRY_FUNDING_8H = 0.00025  # 0.025% / 8h
    EXIT_FUNDING_8H = 0.00005  # 0.005% / 8h
    MIN_INTERVALS = 9  # 3 суток по 8ч
    ROUND_TRIP_MAKER_COST = 0.0008  # Spot + Perp вход и выход (Maker ~0.02% x 4)
    MAX_PAYBACK_INTERVALS = 6.0  # Окупаемость комиссий не дольше 2 суток

    def evaluate_pair(
        self,
        symbol: str,
        funding_history_8h: List[float],
        spot_price: float,
        perp_price: float,
        has_open_carry: bool = False,
        leverage: float = 1.5,
    ) -> CarryOpportunity:
        """
        Оценивает целесообразность открытия, удержания или закрытия дельта-нейтральной
        связки Long Spot + Short Linear Perp.
        `funding_history_8h` упорядочен хронологически (последний элемент — актуальный).
        """
        basis_pct = ((perp_price - spot_price) / spot_price * 100.0) if (spot_price > 0 and perp_price > 0) else 0.0
        safe_lev = max(1.0, min(2.0, float(leverage)))
        reserve_pct = round(max(30.0, (1.0 - 1.0 / max(safe_lev, 1.0)) * 100.0 + 30.0), 1)

        if len(funding_history_8h) < 3:
            return CarryOpportunity(
                symbol=symbol,
                action="SKIP",
                avg_funding_9_8h=0.0,
                avg_funding_3_8h=0.0,
                annualized_apr_pct=0.0,
                basis_pct=round(basis_pct, 4),
                payback_intervals=999.0,
                recommended_leverage=safe_lev,
                margin_reserve_pct=reserve_pct,
                reason="Недостаточно истории фандинга (<3 интервалов)",
            )

        last_3 = funding_history_8h[-3:]
        avg_3 = sum(last_3) / len(last_3)
        last_9 = funding_history_8h[-self.MIN_INTERVALS :]
        avg_9 = sum(last_9) / len(last_9)
        apr = avg_9 * 3.0 * 365.0 * 100.0

        if has_open_carry:
            if funding_history_8h[-1] < 0 or avg_3 < self.EXIT_FUNDING_8H:
                return CarryOpportunity(
                    symbol=symbol,
                    action="CLOSE_CARRY",
                    avg_funding_9_8h=round(avg_9, 6),
                    avg_funding_3_8h=round(avg_3, 6),
                    annualized_apr_pct=round(apr, 2),
                    basis_pct=round(basis_pct, 4),
                    payback_intervals=0.0,
                    recommended_leverage=safe_lev,
                    margin_reserve_pct=reserve_pct,
                    reason=f"Фандинг упал (3-bar avg={avg_3 * 100:.4f}% < 0.005% или < 0)",
                )
            return CarryOpportunity(
                symbol=symbol,
                action="HOLD",
                avg_funding_9_8h=round(avg_9, 6),
                avg_funding_3_8h=round(avg_3, 6),
                annualized_apr_pct=round(apr, 2),
                basis_pct=round(basis_pct, 4),
                payback_intervals=0.0,
                recommended_leverage=safe_lev,
                margin_reserve_pct=reserve_pct,
                reason=f"Удержание carry (APR~{apr:.1f}%)",
            )

        if len(funding_history_8h) < self.MIN_INTERVALS:
            return CarryOpportunity(
                symbol=symbol,
                action="SKIP",
                avg_funding_9_8h=round(avg_9, 6),
                avg_funding_3_8h=round(avg_3, 6),
                annualized_apr_pct=round(apr, 2),
                basis_pct=round(basis_pct, 4),
                payback_intervals=999.0,
                recommended_leverage=safe_lev,
                margin_reserve_pct=reserve_pct,
                reason="Нужно минимум 9 интервалов (3 суток) истории фандинга",
            )

        all_positive = all(f > 0 for f in last_9)
        payback = (self.ROUND_TRIP_MAKER_COST / avg_9) if avg_9 > 0 else 999.0

        if all_positive and avg_9 >= self.MIN_ENTRY_FUNDING_8H and avg_3 >= self.MIN_ENTRY_FUNDING_8H and basis_pct >= -0.05 and payback <= self.MAX_PAYBACK_INTERVALS:
            return CarryOpportunity(
                symbol=symbol,
                action="OPEN_CARRY",
                avg_funding_9_8h=round(avg_9, 6),
                avg_funding_3_8h=round(avg_3, 6),
                annualized_apr_pct=round(apr, 2),
                basis_pct=round(basis_pct, 4),
                payback_intervals=round(payback, 2),
                recommended_leverage=safe_lev,
                margin_reserve_pct=reserve_pct,
                reason=f"Устойчивый фандинг {avg_9 * 100:.4f}%/8ч (APR~{apr:.1f}%, окупаемость {payback:.1f} интерв.)",
            )

        return CarryOpportunity(
            symbol=symbol,
            action="SKIP",
            avg_funding_9_8h=round(avg_9, 6),
            avg_funding_3_8h=round(avg_3, 6),
            annualized_apr_pct=round(apr, 2),
            basis_pct=round(basis_pct, 4),
            payback_intervals=round(payback, 2),
            recommended_leverage=safe_lev,
            margin_reserve_pct=reserve_pct,
            reason="Фандинг ниже порога 0.025%/8ч или нестабилен",
        )

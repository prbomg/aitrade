# -*- coding: utf-8 -*-
"""
Рукав C Модуля «Автотрейдинг» (25% капитала) — Дельта-нейтральный Funding Carry:
- Вход (Long Spot + Short Linear Perp равного номинала) только при устойчивом
  фандинге >= 0.025% / 8h (0.00025) на протяжении >= 9 интервалов (3 суток).
- Плечо <= 2.0x (жёсткий потолок), резерв маржи >= 30%, контроль окупаемости комиссий.
- Защиты от хвостовых рисков 2025-2026: ADL rank >= 4, совпадение залога с валютой
  расчёта (без сторонних обёрток/депега), контроль вводов/выводов и базис <= -0.50%.
- Выход при падении среднего фандинга за последние 3 интервала ниже 0.005% / 8h
  или при переходе текущей ставки в отрицательную зону.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


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
    risk_flags: Tuple[str, ...] = ()


class FundingCarryEngine:
    MIN_ENTRY_FUNDING_8H = 0.00025  # 0.025% / 8h
    EXIT_FUNDING_8H = 0.00005  # 0.005% / 8h
    MIN_INTERVALS = 9  # 3 суток по 8ч
    ROUND_TRIP_MAKER_COST = 0.0008  # Spot + Perp вход и выход (Maker ~0.02% x 4)
    MAX_PAYBACK_INTERVALS = 6.0  # Окупаемость комиссий не дольше 2 суток
    MAX_LEVERAGE = 2.0  # Жёсткий потолок плеча для защиты от ликвидаций/ADL
    MAX_SAFE_ADL_RANK = 3  # При ADL rank >= 4 (из 5) закрываем или не открываем carry
    DEFAULT_MAX_NEGATIVE_BASIS_PCT = -0.50  # Аварийный выход при расхождении базиса <= -0.50%

    def evaluate_pair(
        self,
        symbol: str,
        funding_history_8h: List[float],
        spot_price: float,
        perp_price: float,
        has_open_carry: bool = False,
        leverage: float = 1.5,
        adl_rank: int = 0,
        collateral_matches_settlement: bool = True,
        transfers_enabled: bool = True,
        max_negative_basis_pct: float = DEFAULT_MAX_NEGATIVE_BASIS_PCT,
    ) -> CarryOpportunity:
        """
        Оценивает целесообразность открытия, удержания или закрытия дельта-нейтральной
        связки Long Spot + Short Linear Perp с защитами от хвостовых рисков 2025-2026:
        - Жёсткий лимит плеча <= 2.0x;
        - Блок/выход при высоком ADL-ранге (>= 4);
        - Требование залога в валюте расчёта (защита от депега сторонних стейблов/обёрток);
        - Блок новых входов при приостановке вводов/выводов биржи;
        - Аварийный выход при аномальном отрицательном базисе (каскад ликвидаций).
        `funding_history_8h` упорядочен хронологически (последний элемент — актуальный).
        """
        basis_pct = ((perp_price - spot_price) / spot_price * 100.0) if (spot_price > 0 and perp_price > 0) else 0.0
        req_lev = float(leverage)
        safe_lev = max(1.0, min(self.MAX_LEVERAGE, req_lev))
        reserve_pct = round(max(30.0, (1.0 - 1.0 / max(safe_lev, 1.0)) * 100.0 + 30.0), 1)

        flags: List[str] = []
        if req_lev > self.MAX_LEVERAGE:
            flags.append(f"LEVERAGE_CLAMPED_{req_lev:.1f}x_TO_{self.MAX_LEVERAGE:.1f}x")
        if int(adl_rank) > self.MAX_SAFE_ADL_RANK:
            flags.append(f"HIGH_ADL_RANK_{int(adl_rank)}")
        if not collateral_matches_settlement:
            flags.append("COLLATERAL_MISMATCH")
        if not transfers_enabled:
            flags.append("TRANSFERS_HALTED")
        if basis_pct <= float(max_negative_basis_pct):
            flags.append(f"EXTREME_NEG_BASIS_{basis_pct:.2f}%")

        if len(funding_history_8h) < 3:
            return CarryOpportunity(
                symbol=symbol,
                action="CLOSE_CARRY" if (has_open_carry and (int(adl_rank) > self.MAX_SAFE_ADL_RANK or not collateral_matches_settlement or basis_pct <= float(max_negative_basis_pct))) else "SKIP",
                avg_funding_9_8h=0.0,
                avg_funding_3_8h=0.0,
                annualized_apr_pct=0.0,
                basis_pct=round(basis_pct, 4),
                payback_intervals=999.0,
                recommended_leverage=safe_lev,
                margin_reserve_pct=reserve_pct,
                reason="Аварийный выход по хвостовому риску" if (has_open_carry and flags) else "Недостаточно истории фандинга (<3 интервалов)",
                risk_flags=tuple(flags),
            )

        last_3 = funding_history_8h[-3:]
        avg_3 = sum(last_3) / len(last_3)
        last_9 = funding_history_8h[-self.MIN_INTERVALS :]
        avg_9 = sum(last_9) / len(last_9)
        apr = avg_9 * 3.0 * 365.0 * 100.0

        if has_open_carry:
            if int(adl_rank) > self.MAX_SAFE_ADL_RANK:
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
                    reason=f"Аварийный выход: высокий риск автоделевериджа (ADL rank={int(adl_rank)} >= 4)",
                    risk_flags=tuple(flags),
                )
            if not collateral_matches_settlement:
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
                    reason="Аварийный выход: залог не совпадает с валютой расчёта (риск депега)",
                    risk_flags=tuple(flags),
                )
            if basis_pct <= float(max_negative_basis_pct):
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
                    reason=f"Аварийный выход: аномальный отрицательный базис {basis_pct:.2f}% <= {max_negative_basis_pct:.2f}%",
                    risk_flags=tuple(flags),
                )
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
                    risk_flags=tuple(flags),
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
                risk_flags=tuple(flags),
            )

        # Проверки безопасности перед открытием новой carry-связки
        if not collateral_matches_settlement or not transfers_enabled or int(adl_rank) > self.MAX_SAFE_ADL_RANK:
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
                reason="Вход заблокирован защитой хвостовых рисков: " + ", ".join(flags),
                risk_flags=tuple(flags),
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
                risk_flags=tuple(flags),
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
                risk_flags=tuple(flags),
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
            risk_flags=tuple(flags),
        )

"""
Раздел 4 документа: Математическая система риск-менеджмента (RiskEngine):
1. Расчет размера позиции от фиксированного риска на капитал (Leverage as an Output):
   риск 0.5% от депозита на сделку, жесткий потолок плеча портфеля <= 4.0x.
2. Предварительный расчет проскальзывания по 10 уровням стакана L2 (Dynamic L2 Slippage по формуле Imbalance Labs):
   перевод объемов из токенов в долларовый эквивалент P_i * Q_i и прометание 10 уровней глубины.
3. Правило пятикратной дистанции до ликвидации (5x Stop Distance Rule):
   дистанция от точки входа до цены ликвидации должна быть минимум в 5 раз больше ширины стоп-лосса.
4. Контроль минимального соотношения прибыли к риску (R:R >= 1.5:1) и анти-Мартингейл.
"""

from __future__ import annotations

from typing import Any, Dict

from aitrade.exchange.bybit_v5 import OrderBookSnapshot
from aitrade.strategies.base import TradeSignal


class RiskEngine:
    """Независимый математический фильтр позиционирования, плеча и L2-проскальзывания."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        r_cfg = settings.get("risk", {})
        ex_cfg = settings.get("exchange", {})
        self.risk_per_trade_pct = float(r_cfg.get("risk_per_trade_pct", 0.005))
        self.max_leverage = float(r_cfg.get("max_portfolio_leverage", 4.0))
        self.min_liq_multiple = float(r_cfg.get("min_liquidation_to_stop_multiple", 5.0))
        self.min_rr = float(r_cfg.get("min_reward_to_risk", 1.5))
        self.max_slippage_bps = float(r_cfg.get("max_slippage_bps", 15.0))
        self.maker_fee = float(ex_cfg.get("maker_fee_rate", 0.0002))
        self.taker_fee = float(ex_cfg.get("taker_fee_rate", 0.00055))

    def sweep_l2_slippage_bps(
        self, orderbook: OrderBookSnapshot, side: str, order_notional_usd: float
    ) -> float:
        """
        Формула Imbalance Labs из раздела 4 документа:
        Прометает 10 уровней глубины стакана Level-2 с обязательным переводом объема на каждом уровне i
        из базовых монет Q_i в долларовый эквивалент V_i = P_i * Q_i.
        Возвращает средневзвешенное проскальзывание в базисных пунктах (bps) относительно mid_price.
        """
        mid = orderbook.mid_price
        if mid <= 0 or order_notional_usd <= 0:
            return 0.0

        levels = orderbook.asks[:10] if side == "Buy" else orderbook.bids[:10]
        if not levels:
            return 2.5

        remaining_usd = order_notional_usd
        weighted_bps_sum = 0.0
        filled_usd = 0.0

        for lvl in levels:
            lvl_notional_usd = lvl.price * lvl.size
            take_usd = min(remaining_usd, lvl_notional_usd)
            dist_bps = abs(lvl.price - mid) / mid * 10000.0
            weighted_bps_sum += take_usd * dist_bps
            filled_usd += take_usd
            remaining_usd -= take_usd
            if remaining_usd <= 1e-6:
                break

        # Если 10 уровней стакана не хватило для покрытия ордера, штрафуем остаток удвоенной дистанцией 10-го уровня
        if remaining_usd > 0 and levels:
            worst_bps = abs(levels[-1].price - mid) / mid * 10000.0 * 2.0
            weighted_bps_sum += remaining_usd * worst_bps
            filled_usd += remaining_usd

        return round(weighted_bps_sum / max(filled_usd, 1e-9), 3)

    def size_and_validate_signal(
        self,
        signal: TradeSignal,
        orderbook: OrderBookSnapshot,
        account_equity_usd: float,
    ) -> TradeSignal:
        """
        Вычисляет размер позиции от фиксированного риска (0.5% от капитала),
        проверяет 5x дистанцию до ликвидации и динамическое проскальзывание по 10 уровням стакана L2.
        """
        if signal.entry_price <= 0 or account_equity_usd <= 0:
            return signal

        risk_budget_usd = account_equity_usd * self.risk_per_trade_pct
        stop_frac = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
        stop_frac = max(stop_frac, 0.0015)

        fee_roundtrip_frac = (
            (self.maker_fee * 2.0)
            if signal.order_type == "Limit"
            else (self.taker_fee + self.maker_fee)
        )

        # Первичная оценка долларового размера позиции V_pos = Risk_USD / (stop_frac + fee_roundtrip_frac)
        raw_notional_usd = risk_budget_usd / (stop_frac + fee_roundtrip_frac)

        # Ограничение1: Максимальное плечо портфеля <= 4.0x
        max_notional_by_lev = account_equity_usd * self.max_leverage

        # Ограничение2: Правило 5-кратной дистанции до ликвидации (5x Stop Distance Rule):
        # Дистанция до ликвидации приближенно равна (1 / Leverage) - MMR (0.5%).
        # Чтобы Liq_Dist >= 5 * stop_frac, допустимое эффективное плечо <= 1 / (5 * stop_frac + 0.005).
        max_lev_by_5x_rule = 1.0 / (self.min_liq_multiple * stop_frac + 0.005)
        allowed_leverage = min(self.max_leverage, max_lev_by_5x_rule)
        notional_usd = min(raw_notional_usd, account_equity_usd * allowed_leverage, max_notional_by_lev)

        # Ограничение3: Проверка проскальзывания по 10 уровням L2
        slippage_bps = self.sweep_l2_slippage_bps(orderbook, signal.side, notional_usd)
        if slippage_bps > self.max_slippage_bps:
            scale_down = self.max_slippage_bps / max(slippage_bps, 1e-9)
            notional_usd *= scale_down
            slippage_bps = self.sweep_l2_slippage_bps(orderbook, signal.side, notional_usd)

        effective_lev = max(0.1, notional_usd / account_equity_usd)
        # Расчетная цена ликвидации при выделенной марже с учетом поддерживающей маржи Bybit (MMR = 0.5%)
        liq_dist_frac = max(stop_frac * self.min_liq_multiple, (1.0 / max(effective_lev, 1.0)) - 0.005)
        if signal.side == "Buy":
            liq_price = signal.entry_price * (1.0 - liq_dist_frac)
        else:
            liq_price = signal.entry_price * (1.0 + liq_dist_frac)

        stop_to_liq_mult = liq_dist_frac / stop_frac
        qty = notional_usd / signal.entry_price
        actual_risk_usd = notional_usd * (stop_frac + fee_roundtrip_frac + slippage_bps / 10000.0)

        signal.position_size_usd = round(notional_usd, 2)
        signal.position_qty = round(qty, 4)
        signal.effective_leverage = round(effective_lev, 2)
        signal.liquidation_price = round(max(0.0, liq_price), 6)
        signal.stop_to_liq_multiple = round(stop_to_liq_mult, 2)
        signal.estimated_l2_slippage_bps = slippage_bps
        signal.risk_amount_usd = round(actual_risk_usd, 2)

        if signal.reward_to_risk_tp1 < self.min_rr:
            signal.debate_verdict = "REJECTED"
            signal.debate_summary += f" [RiskEngine: R:R={signal.reward_to_risk_tp1:.2f} < {self.min_rr:.2f}]"

        return signal

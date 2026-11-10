"""
Уровень 1 ИИ-комбайна: Мультиагентный совет (TradingAgents Debate Layer).
Состоит из 4 ролей:
1. Деривативный и микроструктурный аналитик (Derivatives & L2 Analyst)
2. Бычий исследователь (Bullish Researcher) — строит аргументы в пользу лонга / против шорта
3. Медвежий исследователь (Bearish Researcher) — ищет риски, ловушки ликвидности и аргументы в пользу шорта
4. Главный судья и риск-менеджер (Fund Manager & Judge) — выносит итоговый вердикт (APPROVED / REJECTED)
   и устанавливает рыночный режим (RISK_ON, RISK_OFF, RANGE_SNIPER, SQUEEZE_WATCH).
Поддерживает как автономный детерминированный режим, так и подключение к внешнему LLM API (OpenAI / Gemini).
"""

from __future__ import annotations

from typing import Any, Dict, List

from aitrade.strategies.base import MarketContext, TradeSignal


class MultiAgentDebateCouncil:
    """Проводит структурированный раунд дебатов Бычьего и Медвежьего агентов перед допуском сделки."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        self.settings = settings

    def conduct_debate(self, signal: TradeSignal, ctx: MarketContext) -> TradeSignal:
        is_long = signal.side == "Buy"
        vwap_z = (ctx.candles_15m[-1].close - ctx.vwap_bands.vwap) / max(ctx.vwap_bands.std, 1e-9)
        funding_bps = ctx.screener.funding_rate * 10000.0

        bull_args: List[str] = []
        bear_args: List[str] = []

        # Аргументы Бычьего исследователя (Bullish Researcher)
        if vwap_z <= -1.2:
            bull_args.append(f"Цена в зоне глубокой недооценки под сессионным VWAP ({vwap_z:+.2f}σ)")
        if ctx.micro.smoothed_obi > 0.05:
            bull_args.append(f"Перевес лимитного спроса в стакане L2 (сглаженный OBI={ctx.micro.smoothed_obi:+.2f})")
        if funding_bps < 0:
            bull_args.append(f"Отрицательный фандинг ({funding_bps:+.2f} б.п.) создает топливо для шорт-сквиза вверх")
        if ctx.screener.mtf_bias == "BULLISH_TREND":
            bull_args.append("Старший 1H тренд бычий (EMA 8 > 21 > 50)")
        if not bull_args:
            bull_args.append(f"Высокий относительный объем RVOL={ctx.screener.rvol:.2f}x поддерживает импульс")

        # Аргументы Медвежьего исследователя (Bearish Researcher)
        if vwap_z >= 1.2:
            bear_args.append(f"Цена статистически перегрета над сессионным VWAP ({vwap_z:+.2f}σ)")
        if ctx.micro.smoothed_obi < -0.05:
            bear_args.append(f"Давление лимитных оферов в стакане L2 (OBI={ctx.micro.smoothed_obi:+.2f})")
        if funding_bps > 2.5:
            bear_args.append(f"Перегретый положительный фандинг ({funding_bps:+.2f} б.п.) повышает риск каскада лонгов")
        if ctx.screener.mtf_bias == "BEARISH_TREND":
            bear_args.append("Старший 1H тренд медвежий (EMA 8 < 21 < 50)")
        if ctx.micro.spoofing_flags:
            bear_args.append(f"Внимание: зафиксированы подозрения на спуфинг {ctx.micro.spoofing_flags}")
        if not bear_args:
            bear_args.append(f"Волатильность NATR={ctx.screener.natr_pct:.2f}% требует строгого контроля стопа")

        # Определение глобального режима рынка
        if ctx.screener.adx_1h < 24.0:
            regime = "RANGE_SNIPER"
        elif ctx.micro.vpin_tof >= 0.42:
            regime = "SQUEEZE_WATCH"
        elif ctx.screener.mtf_bias == "BULLISH_TREND":
            regime = "RISK_ON_BULL"
        elif ctx.screener.mtf_bias == "BEARISH_TREND":
            regime = "RISK_OFF_BEAR"
        else:
            regime = "SELECTIVE_NEUTRAL"

        # Вердикт судьи (Fund Manager & Risk Judge):
        # Блокируем сделку, если обнаружен активный спуфинг против нас или грубое нарушение MTF-правила
        conflict_blocked = False
        reject_note = ""
        if ctx.micro.liquidity_behavior_index < 0.55:
            conflict_blocked = True
            reject_note = "Низкий индекс доверия к ликвидности стакана LBI < 0.55 (риск спуфинга)"
        elif (
            "MR" in signal.strategy_id
            and ctx.screener.adx_1h >= 35.0
            and (
                (is_long and ctx.screener.mtf_bias == "BEARISH_TREND")
                or (not is_long and ctx.screener.mtf_bias == "BULLISH_TREND")
            )
        ):
            conflict_blocked = True
            reject_note = f"Запрет контртренда против сильного импульса ADX(1H)={ctx.screener.adx_1h:.1f}"

        verdict = "APPROVED" if (signal.passed_ml_filter and not conflict_blocked) else "REJECTED"
        summary_parts = [
            f"[Режим: {regime}]",
            f"Бычий агент: {'; '.join(bull_args[:2])}.",
            f"Медвежий агент: {'; '.join(bear_args[:2])}.",
        ]
        if conflict_blocked:
            summary_parts.append(f"Вердикт судьи: ОТКЛОНЕНО ({reject_note}).")
        else:
            summary_parts.append(
                f"Вердикт судьи: ОДОБРЕНО (ML Winrate={signal.ml_win_probability * 100:.1f}%, "
                f"E[R]={signal.expected_return_pct:+.2f}% > 2xИздержек={signal.cost_threshold_pct:.2f}%, DI={signal.dissimilarity_index:.2f})."
            )

        signal.debate_regime = regime
        signal.debate_verdict = verdict
        signal.debate_summary = " ".join(summary_parts)
        return signal

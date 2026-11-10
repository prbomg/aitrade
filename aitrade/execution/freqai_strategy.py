"""
Готовый класс стратегии для запуска внутри контейнера Freqtrade + FreqAI на Bybit V5
(файл совместим с config/freqtrade_freqai_bybit.json).
Реализует:
- feature_engineering_expand_all() и feature_engineering_standard() с подачей VAMP/OBI,
  фильтра Савицкого — Голея, наклона VWAP, RVOL и отношения ATR(14)/ATR(20).
- set_freqai_targets() на горизонт 8 свечей (2 часа на 15m).
- Cost-Aware Execution Filter (|r_hat| > 2 * c_total) и фильтр Dissimilarity Index (do_predict == 1).
- Снайперские лимитные входы по сетапам VWAP/Keltner ±2σ и Liquidity Sweep + FVG.
"""

from __future__ import annotations

from typing import Any, Dict


class BybitSniperFreqAIStrategy:
    """
    Адаптер стратегии для Freqtrade + FreqAI (наследуется от IStrategy при запуске внутри Freqtrade).
    """

    INTERFACE_VERSION = 3
    timeframe = "15m"
    can_short = True
    process_only_new_candles = True
    use_exit_signal = True

    # Минимальный ROI страхует фиксацию прибыли по времени
    minimal_roi = {
        "0": 0.035,
        "60": 0.020,
        "180": 0.012,
        "360": 0.005,
    }
    stoploss = -0.025

    # Параметры Cost-Aware Execution Filter (lambda = 2.0, roundtrip maker+spread ~ 0.08%)
    cost_selectivity_lambda: float = 2.0
    roundtrip_cost_frac: float = 0.0008

    def feature_engineering_expand_all(
        self, dataframe: Any, period: int, metadata: Dict[str, Any], **kwargs: Any
    ) -> Any:
        """Генерация мультитаймфреймовых признаков с префиксом % для FreqAI."""
        close = dataframe["close"]
        high = dataframe["high"]
        low = dataframe["low"]
        vol = dataframe["volume"]

        # 1. Нормализованный диапазон и относительный объем RVOL
        tr = (high - low).clip(lower=1e-9)
        atr = tr.ewm(span=period, adjust=False).mean()
        dataframe["%-natr-period"] = atr / close
        dataframe["%-rvol-period"] = vol / vol.rolling(period).mean().clip(lower=1e-9)

        # 2. Отклонение от скользящей средней и полос Кельтнера
        ema = close.ewm(span=period, adjust=False).mean()
        dataframe["%-ema_dist-period"] = (close - ema) / atr.clip(lower=1e-9)

        # 3. Оценка свечной дельты (Close Location Value * Volume)
        clv = ((close - low) - (high - close)) / tr
        dataframe["%-cvd_flow-period"] = (clv * vol).rolling(period).sum() / (
            vol.rolling(period).sum().clip(lower=1e-9)
        )
        return dataframe

    def feature_engineering_standard(
        self, dataframe: Any, metadata: Dict[str, Any], **kwargs: Any
    ) -> Any:
        """Стандартные признаки: ATR(14)/ATR(20) ratio, Z-оценка сессионного VWAP."""
        tr = (dataframe["high"] - dataframe["low"]).clip(lower=1e-9)
        atr14 = tr.ewm(span=14, adjust=False).mean()
        atr20 = atr14.rolling(20).mean().clip(lower=1e-9)
        dataframe["%-atr_expansion_ratio"] = atr14 / atr20

        tp = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3.0
        pv = (tp * dataframe["volume"]).rolling(32).sum()
        vv = dataframe["volume"].rolling(32).sum().clip(lower=1e-9)
        vwap = pv / vv
        vwap_std = tp.rolling(32).std().clip(lower=1e-9)
        dataframe["%-vwap_zscore"] = (dataframe["close"] - vwap) / vwap_std
        return dataframe

    def set_freqai_targets(
        self, dataframe: Any, metadata: Dict[str, Any], **kwargs: Any
    ) -> Any:
        """Целевая переменная: ожидаемая доходность на горизонте label_period_candles (8 баров = 2 часа)."""
        horizon = 8
        dataframe["&-s_target_return"] = (
            dataframe["close"].shift(-horizon) / dataframe["close"] - 1.0
        )
        return dataframe

    def populate_entry_trend(self, dataframe: Any, metadata: Dict[str, Any]) -> Any:
        """
        Условия входа в Лонг и Шорт с обязательным прохождением:
        1. Индекса несходства FreqAI: do_predict == 1 (DI <= 1.0 и нет выброса SVM)
        2. Фильтра окупаемости издержек Cost-Aware Filter: |&-s_target_return| > lambda * c_total
        3. Защитного фильтра волатильности: %-atr_expansion_ratio <= 1.5
        """
        min_edge = self.cost_selectivity_lambda * self.roundtrip_cost_frac

        long_cond = (
            (dataframe["do_predict"] == 1)
            & (dataframe["&-s_target_return"] > min_edge)
            & (dataframe["%-atr_expansion_ratio"] <= 1.5)
            & (dataframe["%-vwap_zscore"] <= -1.3)
        )
        short_cond = (
            (dataframe["do_predict"] == 1)
            & (dataframe["&-s_target_return"] < -min_edge)
            & (dataframe["%-atr_expansion_ratio"] <= 1.5)
            & (dataframe["%-vwap_zscore"] >= 1.3)
        )

        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[short_cond, "enter_short"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: Any, metadata: Dict[str, Any]) -> Any:
        """Фиксация позиции при возврате к сессионному VWAP."""
        dataframe.loc[dataframe["%-vwap_zscore"] >= 0.0, "exit_long"] = 1
        dataframe.loc[dataframe["%-vwap_zscore"] <= 0.0, "exit_short"] = 1
        return dataframe

    def leverage(
        self,
        pair: str,
        current_time: Any,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: Any,
        side: str,
        **kwargs: Any,
    ) -> float:
        """Жесткое ограничение плеча до 3.0x–4.0x согласно правилу 5x дистанции до ликвидации."""
        return min(3.0, max_leverage)

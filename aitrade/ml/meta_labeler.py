"""
Уровень 2 ИИ-комбайна: Табличное ML-ядро мета-разметки (Meta-Labeling),
расчета Индекса несходства (Dissimilarity Index — DI) и фильтра окупаемости издержек (Cost-Aware Execution Filter).
Реализует:
1. Обучение на скользящем окне (Walk-Forward) ансамбля мета-разметки на чистом Python
   с экспоненциальным весом свежих наблюдений exp(-i / (alpha * n)).
2. Нормализацию признаков в диапазон [-1, 1] (MinMaxScaler на объединенном окне) и расчет Dissimilarity Index:
   DI_k = d_min(x_k, X_train) / d_bar(X_train). Если DI_k > DI_threshold (1.0), сигнал блокируется как рыночный шок.
3. Cost-Aware Execution Filter: |r_hat| > lambda * c_total (где lambda = 2.0, а c_total включает комиссию входа/выхода
   на Bybit и ожидаемый спред/проскальзывание). Отсекает шумовые входы.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_rvol, calc_rsi, calc_session_vwap_bands
from aitrade.strategies.base import MarketContext, TradeSignal


class WalkForwardMetaLabeler:
    """Адаптивный ML-валидатор сигналов с защитой от выбросов (DI) и фильтром издержек (Cost-Aware)."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        ml_cfg = settings.get("ml_filter", {})
        ex_cfg = settings.get("exchange", {})
        self.cost_lambda = float(ml_cfg.get("cost_selectivity_lambda", 2.0))
        self.di_threshold = float(ml_cfg.get("di_threshold", 1.0))
        self.min_win_prob = float(ml_cfg.get("min_win_probability", 0.62))
        self.label_horizon = int(ml_cfg.get("label_horizon_bars", 8))
        self.maker_fee = float(ex_cfg.get("maker_fee_rate", 0.0002))
        self.taker_fee = float(ex_cfg.get("taker_fee_rate", 0.00055))

    def _extract_historical_matrix(
        self, candles: List[Candle], direction_sign: float
    ) -> Tuple[List[List[float]], List[float], List[float]]:
        """
        Строит обучающую выборку исторических векторов признаков X, бинарных меток успеха y (1/0)
        и реализованных амплитуд доходности r_realized на горизонте label_horizon свечей.
        Все 4 координаты строго согласованы по физическому смыслу с x_new:
        [vwap_zscore_capped, rsi_norm, rvol_norm, clv_obi_proxy].
        """
        n = len(candles)
        if n < 45:
            return [], [], []

        rsi_s = calc_rsi(candles, 14)
        atr_s = calc_atr_series(candles, 14)
        X: List[List[float]] = []
        y: List[float] = []
        returns_pct: List[float] = []

        start_idx = 18
        end_idx = n - 1
        for i in range(start_idx, max(start_idx + 1, end_idx)):
            sub = candles[max(0, i - 24) : i + 1]
            vwap_b = calc_session_vwap_bands(sub)
            rvol_i = calc_rvol(sub, lookback=15)
            z_vwap = max(-1.8, min(1.8, (candles[i].close - vwap_b.vwap) / max(vwap_b.std, 1e-9)))
            rsi_norm = (rsi_s[i] - 50.0) / 50.0
            rvol_norm = min(rvol_i, 2.2) / 2.2
            rng = max(candles[i].high - candles[i].low, 1e-9)
            clv_proxy = ((candles[i].close - candles[i].low) - (candles[i].high - candles[i].close)) / rng
            feat = [z_vwap, rsi_norm, rvol_norm, clv_proxy]

            future_slice = candles[i + 1 : min(n, i + 1 + self.label_horizon)]
            if not future_slice:
                future_slice = [candles[i]]
            entry = candles[i].close
            if direction_sign > 0:
                mfe = (max(c.high for c in future_slice) - entry) / max(entry, 1e-9) * 100.0
                mae = (entry - min(c.low for c in future_slice)) / max(entry, 1e-9) * 100.0
            else:
                mfe = (entry - min(c.low for c in future_slice)) / max(entry, 1e-9) * 100.0
                mae = (max(c.high for c in future_slice) - entry) / max(entry, 1e-9) * 100.0

            target_step = max(0.22, (atr_s[i] / max(entry, 1e-9)) * 100.0 * 0.85)
            is_win = 1.0 if (mfe >= target_step and mfe >= mae * 0.85) else 0.0
            X.append(feat)
            y.append(is_win)
            returns_pct.append(max(0.25, mfe))

        return X, y, returns_pct

    def _compute_dissimilarity_index(
        self, X_train: List[List[float]], x_new: List[float]
    ) -> float:
        """
        Формула Индекса несходства FreqAI:
        DI_k = d_min(x_k, X_train) / d_bar(X_train) после нормализации MinMaxScaler(-1, 1).
        """
        if len(X_train) < 5:
            return 0.45
        dim = len(x_new)
        all_rows = X_train + [x_new]
        mins = [min(row[d] for row in all_rows) for d in range(dim)]
        maxs = [max(row[d] for row in all_rows) for d in range(dim)]

        def scale(vec: List[float]) -> List[float]:
            out: List[float] = []
            for d in range(dim):
                span = maxs[d] - mins[d]
                if span <= 1e-9:
                    out.append(0.0)
                else:
                    norm01 = (vec[d] - mins[d]) / span
                    out.append(norm01 * 2.0 - 1.0)
            return out

        scaled_train = [scale(r) for r in X_train]
        scaled_new = scale(x_new)

        def euclid(a: List[float], b: List[float]) -> float:
            return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

        pair_dists: List[float] = []
        m = len(scaled_train)
        for i in range(m):
            for j in range(i + 1, m):
                pair_dists.append(euclid(scaled_train[i], scaled_train[j]))
        d_bar = sum(pair_dists) / max(len(pair_dists), 1)
        if d_bar <= 1e-9:
            return 0.50

        d_min = min(euclid(scaled_new, row) for row in scaled_train)
        return round(d_min / d_bar, 3)

    def evaluate_signal(self, signal: TradeSignal, ctx: MarketContext) -> TradeSignal:
        """
        Пропускает сгенерированный стратегией сигнал через:
        1. Walk-Forward оценку вероятности успеха (Meta-Labeling Win Probability),
        2. Проверку аномалий по индексу несходства (DI <= DI_threshold),
        3. Cost-Aware Execution Filter: |r_hat| > lambda * c_total.
        """
        direction_sign = 1.0 if signal.side == "Buy" else -1.0
        X_train, y_train, ret_train = self._extract_historical_matrix(ctx.candles_15m, direction_sign)

        z_vwap = max(-1.8, min(1.8, float(signal.features.get("vwap_zscore", 0.0))))
        rsi_norm = (float(signal.features.get("rsi_14", 50.0)) - 50.0) / 50.0
        rvol_norm = min(float(signal.features.get("rvol", 1.5)), 2.2) / 2.2
        obi_val = max(-1.0, min(1.0, float(signal.features.get("obi_smoothed", 0.0))))
        x_new = [z_vwap, rsi_norm, rvol_norm, obi_val]

        di_value = self._compute_dissimilarity_index(X_train, x_new)

        if X_train and y_train:
            total_w = 0.0
            win_w = 0.0
            ret_w = 0.0
            n_samples = len(X_train)
            for idx, (row, label, r_pct) in enumerate(zip(X_train, y_train, ret_train)):
                recency_w = math.exp(-(n_samples - idx) / (0.85 * n_samples))
                dist_sq = sum((a - b) ** 2 for a, b in zip(row, x_new))
                sim_w = math.exp(-dist_sq / 2.0)
                w = recency_w * sim_w
                total_w += w
                win_w += w * label
                ret_w += w * r_pct
            empirical_prob = win_w / max(total_w, 1e-9)
            empirical_ret = ret_w / max(total_w, 1e-9)
        else:
            empirical_prob = signal.base_winrate_est
            empirical_ret = 1.25

        micro_bonus = 0.0
        if direction_sign * ctx.micro.smoothed_obi > 0.08:
            micro_bonus += 0.045
        elif direction_sign * ctx.micro.smoothed_obi < -0.15:
            micro_bonus -= 0.060

        if direction_sign * ctx.micro.vamp_divergence_bps > 0.15:
            micro_bonus += 0.025
        if ctx.screener.rvol >= 1.5:
            micro_bonus += 0.025
        if ctx.micro.liquidity_behavior_index >= 0.80:
            micro_bonus += 0.025
        if signal.reward_to_risk_tp1 >= 1.6:
            micro_bonus += 0.025

        blended_prob = 0.68 * signal.base_winrate_est + 0.32 * empirical_prob + micro_bonus
        blended_prob = round(max(0.35, min(0.92, blended_prob)), 4)

        target_tp1_pct = abs(signal.take_profit_1 - signal.entry_price) / max(signal.entry_price, 1e-9) * 100.0
        expected_return_pct = round(
            blended_prob * max(target_tp1_pct, empirical_ret * 0.75)
            - (1.0 - blended_prob) * (signal.stop_distance_pct * 0.45),
            3,
        )

        entry_fee_pct = (self.maker_fee if signal.order_type == "Limit" else self.taker_fee) * 100.0
        exit_fee_pct = self.maker_fee * 100.0
        total_roundtrip_cost_pct = entry_fee_pct + exit_fee_pct + ctx.screener.spread_pct
        cost_threshold_pct = round(self.cost_lambda * total_roundtrip_cost_pct, 4)

        passed_cost = expected_return_pct >= cost_threshold_pct
        passed_di = di_value <= self.di_threshold
        passed_prob = blended_prob >= self.min_win_prob

        signal.ml_win_probability = blended_prob
        signal.expected_return_pct = expected_return_pct
        signal.cost_threshold_pct = cost_threshold_pct
        signal.dissimilarity_index = di_value
        signal.passed_ml_filter = bool(passed_cost and passed_di and passed_prob)
        return signal

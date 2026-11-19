# -*- coding: utf-8 -*-
"""
Опциональная 3-состоячная модель GaussianHMM (BULL_TREND / SIDEWAYS / BEAR_TREND)
с порогом уверенности P(state) >= 0.60 и детерминированным fallback, если
hmmlearn / scikit-learn / numpy не установлены в окружении.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from aitrade.exchange.bybit_v5 import Candle

try:
    import numpy as np  # type: ignore
    from sklearn.preprocessing import StandardScaler  # type: ignore
    from hmmlearn.hmm import GaussianHMM  # type: ignore

    HMM_AVAILABLE = True
except Exception:
    np = None  # type: ignore
    StandardScaler = None  # type: ignore
    GaussianHMM = None  # type: ignore
    HMM_AVAILABLE = False


@dataclass
class HMMRegimeResult:
    state: str  # BULL_TREND | SIDEWAYS | BEAR_TREND | NO_REGIME
    confidence: float
    backend: str  # gaussian_hmm | unavailable
    raw_state_probs: Tuple[float, float, float] = (0.0, 0.0, 0.0)


class RegimeHMM:
    """
    3-состоячная GaussianHMM на признаках [ret_5d, realized_vol_21d, return_vol_ratio].
    Если библиотеки недоступны или данных недостаточно, возвращает NO_REGIME (backend='unavailable'),
    позволяя regime_router.py опираться на числовую тройку (ADX + ATR% + Hurst).
    """

    CONFIDENCE_GATE = 0.60

    def __init__(self, random_state: int = 42) -> None:
        self.random_state = random_state

    @staticmethod
    def _build_features(bars_1h: List[Candle]) -> List[Tuple[float, float, float]]:
        """
        Строит признаки [ret_5d (120h), realized_vol_21d (504h -> fallback 72h), return_vol_ratio]
        по закрытым 1h барам без заглядывания в будущее.
        """
        closes = [c.close for c in bars_1h]
        n = len(closes)
        if n < 80:
            return []
        ret_win = min(120, n // 3)
        vol_win = min(504, max(48, n // 2))
        if n <= vol_win + 2:
            vol_win = max(24, n // 3)
        start_i = max(ret_win, vol_win)
        feats: List[Tuple[float, float, float]] = []
        log_rets = [0.0] + [
            math.log(max(closes[i], 1e-12) / max(closes[i - 1], 1e-12))
            for i in range(1, n)
        ]
        for i in range(start_i, n):
            ret_5d = (closes[i] / max(closes[i - ret_win], 1e-12)) - 1.0
            rv_slice = log_rets[i - vol_win + 1 : i + 1]
            mean_r = sum(rv_slice) / len(rv_slice)
            var_r = sum((r - mean_r) ** 2 for r in rv_slice) / len(rv_slice)
            realized_vol = math.sqrt(max(var_r, 1e-12)) * math.sqrt(24.0 * 365.0)
            ratio = ret_5d / max(realized_vol, 1e-6)
            feats.append((ret_5d, realized_vol, ratio))
        return feats

    def predict(self, bars_1h: List[Candle]) -> HMMRegimeResult:
        if not HMM_AVAILABLE:
            return HMMRegimeResult(
                state="NO_REGIME",
                confidence=0.0,
                backend="unavailable",
            )
        feats = self._build_features(bars_1h)
        if len(feats) < 60:
            return HMMRegimeResult(
                state="NO_REGIME",
                confidence=0.0,
                backend="unavailable",
            )
        try:
            X = np.asarray(feats, dtype=float)
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            model = GaussianHMM(
                n_components=3,
                covariance_type="full",
                n_iter=100,
                random_state=self.random_state,
            )
            model.fit(X_scaled)
            posteriors = model.predict_proba(X_scaled)
            last_probs = posteriors[-1]
            # Сортировка состояний по среднему ret_5d: min -> BEAR, mid -> SIDEWAYS, max -> BULL
            means_ret = [float(model.means_[idx][0]) for idx in range(3)]
            order = sorted(range(3), key=lambda k: means_ret[k])
            label_map = {
                order[0]: "BEAR_TREND",
                order[1]: "SIDEWAYS",
                order[2]: "BULL_TREND",
            }
            best_idx = int(np.argmax(last_probs))
            best_prob = float(last_probs[best_idx])
            state = label_map[best_idx] if best_prob >= self.CONFIDENCE_GATE else "NO_REGIME"
            return HMMRegimeResult(
                state=state,
                confidence=round(best_prob, 4),
                backend="gaussian_hmm",
                raw_state_probs=(
                    round(float(last_probs[order[0]]), 4),
                    round(float(last_probs[order[1]]), 4),
                    round(float(last_probs[order[2]]), 4),
                ),
            )
        except Exception:
            return HMMRegimeResult(
                state="NO_REGIME",
                confidence=0.0,
                backend="unavailable",
            )

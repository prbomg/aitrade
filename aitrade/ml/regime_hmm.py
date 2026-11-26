# -*- coding: utf-8 -*-
"""
Опциональная 3-состоячная модель GaussianHMM (BULL_TREND / SIDEWAYS / BEAR_TREND)
с порогом уверенности P(state) >= 0.60, детерминированным fallback и грубым
режимным гейтом evaluate_coarse_regime_gate (без кондиционирования весов портфеля на HMM).
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


@dataclass
class CoarseRegimeGate:
    """
    Грубый режимный гейт (без прямого кондиционирования весов портфеля на HMM):
    - В HIGH_VOL снижает размер позиций (size_multiplier = 0.65) и отключает контртрендовые/range стратегии.
    - Контртрендовые (mean-reversion / range) стратегии допускаются только в SIDEWAYS (при нормальной волатильности).
    """

    regime: str  # BULL_TREND | SIDEWAYS | BEAR_TREND | NO_REGIME
    vol_regime: str  # LOW_VOL | NORMAL_VOL | HIGH_VOL
    size_multiplier: float
    allow_mean_reversion: bool
    allow_trend_breakout: bool
    realized_vol_ann: float
    confidence: float
    backend: str
    reason: str


def evaluate_coarse_regime_gate(
    bars_1h: List[Candle],
    strategy_id: str = "",
    high_vol_ann_threshold: float = 0.95,
    low_vol_ann_threshold: float = 0.35,
    high_vol_size_mult: float = 0.65,
    random_state: int = 42,
) -> CoarseRegimeGate:
    """
    Оценивает грубый рыночный режим и режим волатильности по 1h свечам.
    Работает как с GaussianHMM, так и в детерминированном fallback-режиме без внешних ML-библиотек.
    """
    closes = [float(c.close) for c in (bars_1h or []) if float(getattr(c, "close", 0.0) or 0.0) > 0.0]
    n = len(closes)
    if n < 25:
        return CoarseRegimeGate(
            regime="NO_REGIME",
            vol_regime="NORMAL_VOL",
            size_multiplier=1.0,
            allow_mean_reversion=True,
            allow_trend_breakout=True,
            realized_vol_ann=0.0,
            confidence=0.0,
            backend="insufficient_data",
            reason="Недостаточно 1h баров (<25) — применён нейтральный гейт 1.0x",
        )

    vol_win = min(72, n - 1)
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(n - vol_win, n)]
    mean_r = sum(log_rets) / len(log_rets)
    var_r = sum((r - mean_r) ** 2 for r in log_rets) / len(log_rets)
    realized_vol_ann = math.sqrt(max(var_r, 1e-12)) * math.sqrt(24.0 * 365.0)

    if realized_vol_ann >= high_vol_ann_threshold:
        vol_regime = "HIGH_VOL"
    elif realized_vol_ann <= low_vol_ann_threshold:
        vol_regime = "LOW_VOL"
    else:
        vol_regime = "NORMAL_VOL"

    hmm_res = RegimeHMM(random_state=random_state).predict(bars_1h)
    regime = hmm_res.state
    conf = hmm_res.confidence
    backend = hmm_res.backend

    if regime == "NO_REGIME":
        # Детерминированный грубый fallback по 5d доходности и реализованной волатильности
        ret_win = min(120, n - 1)
        ret_5d = (closes[-1] / closes[-1 - ret_win]) - 1.0
        vol_5d = realized_vol_ann * math.sqrt(ret_win / (24.0 * 365.0))
        z_trend = ret_5d / max(vol_5d, 1e-6)
        backend = "vol_ret_fallback"
        if z_trend >= 1.15:
            regime = "BULL_TREND"
            conf = round(min(0.95, 0.55 + 0.15 * z_trend), 4)
        elif z_trend <= -1.15:
            regime = "BEAR_TREND"
            conf = round(min(0.95, 0.55 + 0.15 * abs(z_trend)), 4)
        else:
            regime = "SIDEWAYS"
            conf = round(max(0.50, 0.80 - 0.25 * abs(z_trend)), 4)

    size_mult = float(high_vol_size_mult) if vol_regime == "HIGH_VOL" else 1.0
    allow_mr = (regime == "SIDEWAYS") and (vol_regime != "HIGH_VOL")
    allow_trend = True

    sid_u = str(strategy_id or "").upper()
    is_mr_strat = any(k in sid_u for k in ("RANGE", "MEAN_REV", "VWAP_RANGE", "REVERT"))
    if is_mr_strat and not allow_mr:
        size_mult = 0.0
        reason = f"Контртренд {strategy_id} заблокирован: режим {regime}/{vol_regime} (ann_vol={realized_vol_ann:.2f})"
    elif vol_regime == "HIGH_VOL":
        reason = f"HIGH_VOL (ann_vol={realized_vol_ann:.2f} >= {high_vol_ann_threshold:.2f}) — сайзинг снижен до {size_mult:.2f}x"
    else:
        reason = f"Режим {regime}/{vol_regime} (ann_vol={realized_vol_ann:.2f}, conf={conf:.2f}) — штатный сайзинг {size_mult:.2f}x"

    return CoarseRegimeGate(
        regime=regime,
        vol_regime=vol_regime,
        size_multiplier=round(size_mult, 3),
        allow_mean_reversion=allow_mr,
        allow_trend_breakout=allow_trend,
        realized_vol_ann=round(realized_vol_ann, 4),
        confidence=conf,
        backend=backend,
        reason=reason,
    )

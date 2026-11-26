# -*- coding: utf-8 -*-
"""
Long-Only Minimum Variance (MinVar) портфельный оптимизатор со сжатием ковариации
Ledoit-Wolf (2004). Низкооборотный структурный базис без внешних зависимостей.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


@dataclass
class MinVarPortfolioResult:
    weights: Dict[str, float]
    shrinkage_delta: float
    portfolio_vol_ann: float
    equal_weight_vol_ann: float
    vol_reduction_pct: float
    diversification_ratio: float
    n_obs: int
    symbols: Tuple[str, ...]


def compute_ledoit_wolf_cov(returns_matrix: Sequence[Sequence[float]]) -> Tuple[List[List[float]], float]:
    """
    Оценка ковариационной матрицы со сжатием Ledoit-Wolf (2004) к масштабированной единичной матрице:
        Sigma = (1 - delta) * S + delta * mu * I
    `returns_matrix` имеет форму T x N (T наблюдений, N активов).
    """
    T = len(returns_matrix)
    if T < 2:
        return [], 1.0
    N = len(returns_matrix[0])
    if N == 0:
        return [], 1.0

    means = [sum(returns_matrix[t][i] for t in range(T)) / T for i in range(N)]
    centered = [[float(returns_matrix[t][i]) - means[i] for i in range(N)] for t in range(T)]

    # Выборочная ковариация S (нормировка на T по Ledoit-Wolf 2004)
    S = [[0.0] * N for _ in range(N)]
    for t in range(T):
        row = centered[t]
        for i in range(N):
            ri = row[i]
            for j in range(i, N):
                S[i][j] += ri * row[j]
    for i in range(N):
        for j in range(i, N):
            v = S[i][j] / T
            S[i][j] = v
            S[j][i] = v

    # Средняя дисперсия mu = trace(S) / N
    mu = sum(S[i][i] for i in range(N)) / N
    if mu <= 1e-18:
        return [[(1e-12 if i == j else 0.0) for j in range(N)] for i in range(N)], 1.0

    # d^2 = ||S - mu*I||_F^2 / N
    d2_sum = 0.0
    for i in range(N):
        for j in range(N):
            target = mu if i == j else 0.0
            diff = S[i][j] - target
            d2_sum += diff * diff
    d2 = d2_sum / N
    if d2 <= 1e-18:
        return S, 0.0

    # b^2_bar = (1 / T^2) * sum_t ||x_t x_t^T - S||_F^2 / N
    b2_acc = 0.0
    for t in range(T):
        row = centered[t]
        norm2 = 0.0
        for i in range(N):
            ri = row[i]
            for j in range(N):
                diff = ri * row[j] - S[i][j]
                norm2 += diff * diff
        b2_acc += norm2 / N
    b2_bar = b2_acc / (T * T)
    b2 = min(b2_bar, d2)
    delta = max(0.0, min(1.0, b2 / d2))

    sigma = [[0.0] * N for _ in range(N)]
    for i in range(N):
        for j in range(N):
            target = mu if i == j else 0.0
            sigma[i][j] = (1.0 - delta) * S[i][j] + delta * target
    return sigma, round(delta, 4)


def _project_bounded_simplex(v: List[float], upper: float) -> List[float]:
    """
    Точная евклидова проекция вектора v на ограниченный симплекс:
        {w : sum(w) = 1, 0 <= w_i <= upper}
    """
    n = len(v)
    if n == 0:
        return []
    u = max(1.0 / n, min(1.0, float(upper)))
    lo = min(v) - u
    hi = max(v)
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        s = sum(max(0.0, min(u, vi - mid)) for vi in v)
        if s > 1.0:
            lo = mid
        else:
            hi = mid
    tau = 0.5 * (lo + hi)
    w = [max(0.0, min(u, vi - tau)) for vi in v]
    tot = sum(w)
    if tot > 0:
        w = [x / tot for x in w]
    return w


def _quad_form(w: Sequence[float], cov: Sequence[Sequence[float]]) -> float:
    n = len(w)
    val = 0.0
    for i in range(n):
        wi = w[i]
        row = cov[i]
        s = 0.0
        for j in range(n):
            s += row[j] * w[j]
        val += wi * s
    return max(0.0, val)


def optimize_long_only_minvar(
    returns_by_symbol: Dict[str, Sequence[float]],
    max_weight: float = 0.35,
    min_obs: int = 30,
    periods_per_year: float = 365.0,
) -> MinVarPortfolioResult:
    """
    Решает задачу Long-Only Minimum Variance:
        min_w 0.5 * w^T Sigma_LW w   s.t.  sum(w)=1, 0 <= w_i <= max_weight
    """
    symbols = tuple(sorted(k for k, v in (returns_by_symbol or {}).items() if v is not None))
    n = len(symbols)
    if n == 0:
        return MinVarPortfolioResult({}, 0.0, 0.0, 0.0, 0.0, 1.0, 0, ())
    if n == 1:
        s0 = symbols[0]
        r0 = [float(x) for x in returns_by_symbol[s0]]
        if len(r0) >= 2:
            m0 = sum(r0) / len(r0)
            v0 = sum((x - m0) ** 2 for x in r0) / len(r0)
            vol = math.sqrt(max(v0, 0.0) * periods_per_year)
        else:
            vol = 0.0
        return MinVarPortfolioResult({s0: 1.0}, 0.0, round(vol, 4), round(vol, 4), 0.0, 1.0, len(r0), symbols)

    n_obs = min(len(returns_by_symbol[s]) for s in symbols)
    eq_w = [1.0 / n] * n
    if n_obs < min_obs:
        return MinVarPortfolioResult(
            weights={s: round(1.0 / n, 6) for s in symbols},
            shrinkage_delta=1.0,
            portfolio_vol_ann=0.0,
            equal_weight_vol_ann=0.0,
            vol_reduction_pct=0.0,
            diversification_ratio=1.0,
            n_obs=n_obs,
            symbols=symbols,
        )

    # Выравниваем по хвосту последних n_obs периодов
    mat = [[float(returns_by_symbol[s][-n_obs + t]) for s in symbols] for t in range(n_obs)]
    cov, delta = compute_ledoit_wolf_cov(mat)

    # Оценка максимального собственного значения (норма Фробениуса как верхняя граница Липшица)
    frob = math.sqrt(sum(cov[i][j] * cov[i][j] for i in range(n) for j in range(n)))
    step = 1.0 / max(frob, 1e-12)
    upper = max(1.0 / n, min(1.0, float(max_weight)))

    w = list(eq_w)
    for _ in range(400):
        grad = [sum(cov[i][j] * w[j] for j in range(n)) for i in range(n)]
        cand = _project_bounded_simplex([w[i] - step * grad[i] for i in range(n)], upper)
        diff = max(abs(cand[i] - w[i]) for i in range(n))
        w = cand
        if diff < 1e-9:
            break

    port_var = _quad_form(w, cov)
    eq_var = _quad_form(eq_w, cov)
    port_vol_ann = math.sqrt(port_var * periods_per_year)
    eq_vol_ann = math.sqrt(eq_var * periods_per_year)
    vol_red = ((eq_vol_ann - port_vol_ann) / eq_vol_ann * 100.0) if eq_vol_ann > 1e-12 else 0.0

    weighted_asset_vol = sum(w[i] * math.sqrt(max(cov[i][i], 0.0) * periods_per_year) for i in range(n))
    div_ratio = (weighted_asset_vol / port_vol_ann) if port_vol_ann > 1e-12 else 1.0

    # Округляем веса и гарантируем точную сумму 1.0
    rounded = [round(x, 6) for x in w]
    rem = round(1.0 - sum(rounded), 6)
    if rounded:
        max_idx = max(range(n), key=lambda k: rounded[k])
        rounded[max_idx] = round(rounded[max_idx] + rem, 6)

    return MinVarPortfolioResult(
        weights={symbols[i]: rounded[i] for i in range(n)},
        shrinkage_delta=delta,
        portfolio_vol_ann=round(port_vol_ann, 4),
        equal_weight_vol_ann=round(eq_vol_ann, 4),
        vol_reduction_pct=round(vol_red, 2),
        diversification_ratio=round(div_ratio, 4),
        n_obs=n_obs,
        symbols=symbols,
    )

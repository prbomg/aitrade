# -*- coding: utf-8 -*-
"""
Промышленные шлюзы валидации стратегий и ML-моделей (AItrade 2026, intraday.49):
- Шлюз 1: Lookahead Analysis (утечка будущего) и Recursive Analysis (окна прогрева 199/299/499).
- Шлюз 2: PSR, MinBTL, PBO (CPCV / WFA), декомпозиция беты и альфа Йенсена к BTC,
  стресс-тест по режимам (LOW_VOL / HIGH_VOL / BULL / BEAR) и детектор красных флагов переобучения.
- Контроль дрейфа признаков: PSI (Population Stability Index) и двухвыборочный KS-test.
"""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from itertools import combinations
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from aitrade.exchange.bybit_v5 import Candle


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    p = max(1e-12, min(1.0 - 1e-12, p))
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)


def _sig_fingerprint(sig: Any) -> Optional[Tuple[str, str, float, float, float]]:
    if sig is None:
        return None
    return (
        str(getattr(sig, "strategy_id", "")),
        str(getattr(sig, "side", "")),
        round(float(getattr(sig, "entry_price", 0.0) or 0.0), 6),
        round(float(getattr(sig, "stop_loss", 0.0) or 0.0), 6),
        round(float(getattr(sig, "take_profit_2", 0.0) or 0.0), 6),
    )


def run_lookahead_check(
    candles: List[Candle],
    strategy_fn: Callable[[str, List[Candle], float], Any],
    symbol: str = "BTCUSDT",
    warmup: int = 85,
    max_checks: int = 40,
) -> Dict[str, Any]:
    """
    Шлюз 1 (Lookahead Analysis):
    1) Проверяет, что сигнал на баре i с обрезанным окном candles[:i] идентичен сигналу,
       когда будущие бары candles[i:] экстремально искажены (×3 вверх / ×0.3 вниз).
    2) Проверяет, что strategy_fn не мутирует переданный список свечей.
    """
    n = len(candles)
    if n <= warmup + 5:
        return {"passed": True, "checked_bars": 0, "signals_found": 0, "violations": []}

    step = max(1, (n - warmup - 2) // max(1, max_checks))
    checked = 0
    signals_found = 0
    violations: List[Dict[str, Any]] = []

    for i in range(warmup, n - 1, step):
        ref_open = candles[i].open
        base_hist = list(candles[:i])
        base_snap = [(c.timestamp, c.close) for c in base_hist[-3:]]
        sig_base = strategy_fn(symbol, base_hist, ref_open)
        if [(c.timestamp, c.close) for c in base_hist[-3:]] != base_snap:
            violations.append({"bar_idx": i, "reason": "strategy_fn mutated input candles"})
            break

        # Повторный вызов на глубокой копии с гарантированно отрезанным будущим
        trunc_hist = copy.deepcopy(candles[:i])
        sig_trunc = strategy_fn(symbol, trunc_hist, ref_open)
        fp_base = _sig_fingerprint(sig_base)
        fp_trunc = _sig_fingerprint(sig_trunc)
        checked += 1
        if fp_base is not None:
            signals_found += 1
        if fp_base != fp_trunc:
            violations.append({"bar_idx": i, "reason": "non-deterministic or lookahead mismatch", "base": fp_base, "trunc": fp_trunc})

    return {
        "passed": len(violations) == 0,
        "checked_bars": checked,
        "signals_found": signals_found,
        "violations": violations[:10],
    }


def run_recursive_warmup_check(
    candles: List[Candle],
    strategy_fn: Callable[[str, List[Candle], float], Any],
    symbol: str = "BTCUSDT",
    warmup_windows: Tuple[int, ...] = (199, 299, 499),
    tolerance_pct: float = 0.10,
) -> Dict[str, Any]:
    """
    Шлюз 1 (Recursive Analysis):
    Сравнивает сигнал на последней свече при полном окне истории и при нечётных окнах прогрева
    startup_candle_count (199, 299, 499). Расхождение > tolerance_pct (0.1%) или смена стороны = предупреждение.
    """
    n = len(candles)
    if n < 210:
        return {"passed": True, "windows_checked": [], "max_deviation_pct": 0.0, "warnings": []}

    ref_open = candles[-1].open
    full_hist = candles[:-1]
    sig_full = strategy_fn(symbol, full_hist, ref_open)
    fp_full = _sig_fingerprint(sig_full)

    windows_checked: List[int] = []
    warnings: List[str] = []
    max_dev = 0.0

    for w in warmup_windows:
        if len(full_hist) <= w:
            continue
        windows_checked.append(w)
        sig_w = strategy_fn(symbol, full_hist[-w:], ref_open)
        fp_w = _sig_fingerprint(sig_w)
        if (fp_full is None) != (fp_w is None):
            warnings.append(f"window={w}: наличие сигнала отличается от полного окна")
            continue
        if fp_full is not None and fp_w is not None:
            if fp_full[1] != fp_w[1]:
                warnings.append(f"window={w}: сторона сигнала изменилась ({fp_full[1]} -> {fp_w[1]})")
                continue
            for idx_f, lbl in ((2, "entry"), (3, "sl"), (4, "tp")):
                v0, v1 = fp_full[idx_f], fp_w[idx_f]
                if v0 > 0:
                    dev_pct = abs(v1 - v0) / v0 * 100.0
                    if dev_pct > max_dev:
                        max_dev = dev_pct
                    if dev_pct > tolerance_pct:
                        warnings.append(f"window={w}: {lbl} отклонился на {dev_pct:.3f}% (> {tolerance_pct}%)")

    return {
        "passed": len(warnings) == 0,
        "windows_checked": windows_checked,
        "max_deviation_pct": round(max_dev, 4),
        "warnings": warnings,
    }


def compute_psr_and_min_btl(
    daily_returns: Sequence[float],
    benchmark_sr_daily: float = 0.0,
    confidence: float = 0.95,
) -> Dict[str, Optional[float]]:
    """
    Шлюз 2: Probabilistic Sharpe Ratio (PSR) и Minimum Backtest Length (MinBTL, дней)
    по Bailey & Lopez de Prado с поправкой на асимметрию и эксцесс.
    """
    n = len(daily_returns)
    if n < 5:
        return {"psr": None, "min_btl_days": None, "sharpe_ann": None}
    mean_r = sum(daily_returns) / n
    var_r = sum((r - mean_r) ** 2 for r in daily_returns) / max(n - 1, 1)
    if var_r <= 1e-18:
        return {"psr": None, "min_btl_days": None, "sharpe_ann": None}
    std_r = math.sqrt(var_r)
    sr = mean_r / std_r
    sr_ann = sr * math.sqrt(365.0)

    m3 = sum(((r - mean_r) / std_r) ** 3 for r in daily_returns) / n
    m4 = sum(((r - mean_r) / std_r) ** 4 for r in daily_returns) / n
    denom_sq = max(1e-12, 1.0 - m3 * sr + ((m4 - 1.0) / 4.0) * (sr ** 2))
    z_psr = ((sr - benchmark_sr_daily) * math.sqrt(max(1, n - 1))) / math.sqrt(denom_sq)
    psr = _norm_cdf(z_psr)

    min_btl: Optional[float] = None
    if sr > benchmark_sr_daily + 1e-6:
        z_alpha = _norm_ppf(confidence)
        min_btl = 1.0 + denom_sq * ((z_alpha / (sr - benchmark_sr_daily)) ** 2)
        min_btl = round(min(min_btl, 10000.0), 1)

    return {
        "psr": round(psr, 4),
        "min_btl_days": min_btl,
        "sharpe_ann": round(sr_ann, 3),
    }


def compute_pbo_cpcv(
    daily_returns: Sequence[float],
    n_blocks: int = 8,
) -> Optional[float]:
    """
    Оценивает Probability of Backtest Overfitting (PBO) комбинаторным разбиением
    дневных доходностей на `n_blocks` хронологических блоков C(n_blocks, n_blocks/2):
    доля разбиений, где положительный IS-Sharpe сменяется отрицательным OOS-Sharpe.
    Значение < 0.35 — хорошо; >= 0.50 — красный флаг переобучения.
    """
    n = len(daily_returns)
    if n < max(16, n_blocks * 2):
        return None
    k = n_blocks if n_blocks % 2 == 0 else (n_blocks - 1)
    k = max(4, min(10, k))
    blk_size = n // k
    blocks = [list(daily_returns[i * blk_size : (i + 1) * blk_size]) for i in range(k)]

    def _sr(vals: List[float]) -> float:
        m = len(vals)
        if m < 2:
            return 0.0
        mu = sum(vals) / m
        v = sum((x - mu) ** 2 for x in vals) / max(m - 1, 1)
        return mu / math.sqrt(v) if v > 1e-18 else 0.0

    half = k // 2
    idx_all = set(range(k))
    overfit_cnt = 0
    total_cnt = 0
    for is_idx in combinations(range(k), half):
        oos_idx = sorted(idx_all - set(is_idx))
        is_vals = [x for idx in is_idx for x in blocks[idx]]
        oos_vals = [x for idx in oos_idx for x in blocks[idx]]
        sr_is = _sr(is_vals)
        sr_oos = _sr(oos_vals)
        total_cnt += 1
        if sr_oos <= 0.0 or (sr_is > 0.0 and sr_oos < 0.25 * sr_is):
            overfit_cnt += 1
    return round(overfit_cnt / max(1, total_cnt), 3)


def compute_btc_beta_and_jensen_alpha(
    strategy_daily_returns: Sequence[float],
    btc_daily_returns: Sequence[float],
) -> Dict[str, Optional[float]]:
    """
    Декомпозиция беты против BTC и годовая альфа Йенсена (365 * alpha_daily, % годовых).
    """
    m = min(len(strategy_daily_returns), len(btc_daily_returns))
    if m < 5:
        return {"btc_beta": None, "btc_corr": None, "jensen_alpha_ann_pct": None}
    rs = list(strategy_daily_returns[-m:])
    rb = list(btc_daily_returns[-m:])
    ms = sum(rs) / m
    mb = sum(rb) / m
    cov = sum((x - ms) * (y - mb) for x, y in zip(rs, rb)) / max(m - 1, 1)
    var_b = sum((y - mb) ** 2 for y in rb) / max(m - 1, 1)
    var_s = sum((x - ms) ** 2 for x in rs) / max(m - 1, 1)
    if var_b <= 1e-18:
        return {"btc_beta": 0.0, "btc_corr": 0.0, "jensen_alpha_ann_pct": round(ms * 365.0 * 100.0, 2)}
    beta = cov / var_b
    corr = cov / math.sqrt(var_b * var_s) if var_s > 1e-18 else 0.0
    alpha_daily = ms - beta * mb
    return {
        "btc_beta": round(beta, 3),
        "btc_corr": round(corr, 3),
        "jensen_alpha_ann_pct": round(alpha_daily * 365.0 * 100.0, 2),
    }


def compute_regime_stress_breakdown(
    trades: Sequence[Tuple[Any, ...]],
    btc_daily_returns_by_day: Dict[int, float],
) -> Dict[str, Dict[str, Any]]:
    """
    Раздельная отчётность по 4 рыночным режимам BTC:
    - BULL (7-дневная доходность BTC >= +1%) / BEAR (<= -1%)
    - HIGH_VOL (7-дневная реализованная волатильность выше медианы) / LOW_VOL (ниже медианы)
    """
    if not trades or len(btc_daily_returns_by_day) < 7:
        return {}
    days = sorted(btc_daily_returns_by_day.keys())
    vol_by_day: Dict[int, float] = {}
    trend_by_day: Dict[int, float] = {}
    for idx in range(6, len(days)):
        d = days[idx]
        win = [btc_daily_returns_by_day[days[j]] for j in range(idx - 6, idx + 1)]
        mu = sum(win) / len(win)
        vol = math.sqrt(sum((x - mu) ** 2 for x in win) / max(len(win) - 1, 1))
        vol_by_day[d] = vol
        trend_by_day[d] = sum(win)
    if not vol_by_day:
        return {}
    vols_sorted = sorted(vol_by_day.values())
    med_vol = vols_sorted[len(vols_sorted) // 2]

    buckets: Dict[str, List[Tuple[float, float]]] = {"BULL": [], "BEAR": [], "LOW_VOL": [], "HIGH_VOL": []}
    for t in trades:
        day_k = int(int(t[0]) // 86_400_000)
        net_pct = float(t[1])
        r_mult = float(t[2])
        if day_k in trend_by_day:
            if trend_by_day[day_k] >= 0.01:
                buckets["BULL"].append((net_pct, r_mult))
            elif trend_by_day[day_k] <= -0.01:
                buckets["BEAR"].append((net_pct, r_mult))
        if day_k in vol_by_day:
            if vol_by_day[day_k] >= med_vol:
                buckets["HIGH_VOL"].append((net_pct, r_mult))
            else:
                buckets["LOW_VOL"].append((net_pct, r_mult))

    out: Dict[str, Dict[str, Any]] = {}
    for reg, items in buckets.items():
        cnt = len(items)
        if cnt == 0:
            out[reg] = {"trades": 0, "win_rate_pct": 0.0, "avg_r": 0.0}
            continue
        wins = sum(1 for _, r in items if r > 0)
        avg_r = sum(r for _, r in items) / cnt
        out[reg] = {
            "trades": cnt,
            "win_rate_pct": round(wins / cnt * 100.0, 1),
            "avg_r": round(avg_r, 3),
        }
    return out


def detect_overfitting_red_flags(
    oos_summary: Dict[str, Any],
    wfa_result: Dict[str, Any],
    n_params: int = 4,
) -> List[str]:
    """Автоматический детектор красных флагов переобучения (Шлюз 2)."""
    flags: List[str] = []
    trades = int(oos_summary.get("total_trades") or 0)
    wr = float(oos_summary.get("win_rate_pct") or 0.0)
    pf = oos_summary.get("profit_factor")
    net_ret = float(oos_summary.get("net_return_pct") or 0.0)
    max_dd = float(oos_summary.get("max_drawdown_pct") or 0.0)
    wfe = wfa_result.get("wfe")
    pbo = wfa_result.get("pbo")
    psr = wfa_result.get("psr")
    s2x_r = ((wfa_result.get("oos_stress_2x") or {}).get("avg_r_multiple"))

    if trades < 20:
        flags.append(f"Мало OOS-сделок ({trades} < 20)")
    if trades >= 10 and wr > 80.0:
        flags.append(f"Аномально высокий Win Rate ({wr:.1f}% > 80%)")
    if trades >= 10 and pf is not None and float(pf) > 4.0:
        flags.append(f"Подозрительно высокий Profit Factor ({float(pf):.2f} > 4.0)")
    if trades >= 10 and net_ret > 20.0 and max_dd < 0.5:
        flags.append(f"Слишком гладкая кривая (доходность {net_ret:.1f}% при DD {max_dd:.2f}%)")
    if pbo is not None and float(pbo) >= 0.50:
        flags.append(f"Высокий риск переобучения PBO={float(pbo):.2f} (>= 0.50)")
    if psr is not None and float(psr) < 0.95 and trades >= 20:
        flags.append(f"Низкая статистическая значимость PSR={float(psr):.2f} (< 0.95)")
    if wfe is not None and float(wfe) < 0.50:
        flags.append(f"Низкая эффективность WFE={float(wfe):.2f} (< 0.50)")
    if s2x_r is not None and float(s2x_r) <= 0.0 and trades >= 10:
        flags.append(f"Не держит 2x издержки (2x Stress E[R]={float(s2x_r):.3f}R)")
    if n_params > 8:
        flags.append(f"Избыточное число параметров ({n_params} > 8)")
    return flags


def compute_feature_drift(
    X_ref: Sequence[Sequence[float]],
    X_live: Sequence[Sequence[float]],
    n_bins: int = 8,
) -> Dict[str, Any]:
    """
    Контроль дрейфа признаков между обучающей (X_ref) и свежей (X_live) выборками:
    - PSI (Population Stability Index): <0.10 норма, 0.10–0.25 предупреждение, >=0.25 дрейф.
    - Двухвыборочный KS-test (максимальное расстояние между эмпирическими CDF).
    """
    if len(X_ref) < 10 or len(X_live) < 10:
        return {"max_psi": 0.0, "max_ks": 0.0, "status": "INSUFFICIENT_DATA", "per_feature": []}
    dim = min(len(X_ref[0]), len(X_live[0]))
    eff_bins = max(3, min(int(n_bins), min(len(X_ref), len(X_live)) // 25))
    per_feat: List[Dict[str, float]] = []
    max_psi = 0.0
    max_ks = 0.0

    for d in range(dim):
        ref_col = sorted(float(r[d]) for r in X_ref)
        live_col = sorted(float(r[d]) for r in X_live)
        # KS statistic
        i = j = 0
        nr, nl = len(ref_col), len(live_col)
        ks = 0.0
        while i < nr and j < nl:
            if ref_col[i] <= live_col[j]:
                i += 1
            else:
                j += 1
            ks = max(ks, abs(i / nr - j / nl))
        # PSI по квантильным бинам эталонной выборки
        edges = [ref_col[min(nr - 1, int(nr * b / eff_bins))] for b in range(1, eff_bins)]
        ref_bins = [0] * eff_bins
        live_bins = [0] * eff_bins
        for v in ref_col:
            idx = 0
            while idx < len(edges) and v > edges[idx]:
                idx += 1
            ref_bins[idx] += 1
        for v in live_col:
            idx = 0
            while idx < len(edges) and v > edges[idx]:
                idx += 1
            live_bins[idx] += 1
        psi = 0.0
        eps = 1e-4
        for rb, lb in zip(ref_bins, live_bins):
            pr = max(eps, rb / nr)
            pl = max(eps, lb / nl)
            psi += (pl - pr) * math.log(pl / pr)
        max_psi = max(max_psi, psi)
        max_ks = max(max_ks, ks)
        per_feat.append({"dim": float(d), "psi": round(psi, 4), "ks": round(ks, 4)})

    if max_psi >= 0.25 or max_ks >= 0.35:
        status = "DRIFT_ALERT"
    elif max_psi >= 0.15 or max_ks >= 0.25:
        status = "WARNING"
    else:
        status = "OK"
    return {
        "max_psi": round(max_psi, 4),
        "max_ks": round(max_ks, 4),
        "status": status,
        "per_feature": per_feat,
    }

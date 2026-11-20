"""
Многомесячный Point-in-Time и Rolling Walk-Forward бэктестер (AItrade 2026):
- Загрузка 180–365 дней истории свечей Bybit V5 с пагинацией по `end` и локальным кэшем (`data/candles_cache/`).
- Решения строго по закрытым барам candles[:i], вход по Open следующего бара или реальному ретесту.
- Rolling Walk-Forward (6–8 независимых циклов, 75% IS / 25% OOS) с расчётом:
  * WFE (Walk-Forward Efficiency = Annualized OOS / Annualized IS),
  * честного дневного Sharpe Ratio и Deflated Sharpe Ratio (DSR с учётом N_trials),
  * стресс-теста 2x издержек (удвоенные комиссии и проскальзывание),
  * Монте-Карло перестановки порядка сделок (5 000 итераций -> медианный и P95 Max DD).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from aitrade.config import BASE_DIR, load_settings
from aitrade.exchange.bybit_v5 import BybitV5Client, Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.storage import atomic_json
from aitrade.strategies.regime_s1_s3 import (
    MIN_STOP_FRAC,
    generate_s1_s3_signal,
)


@dataclass
class BacktestSummary:
    mode_name: str
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: Optional[float]
    net_return_pct: float
    avg_r_multiple: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]
    deflated_sharpe_ratio: Optional[float]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Аппроксимация квантили стандартного нормального распределения (Acklam)."""
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


def compute_daily_sharpe_and_dsr(
    daily_returns: List[float], n_trials: int = 1
) -> Tuple[Optional[float], Optional[float]]:
    """
    Рассчитывает годовой Sharpe Ratio (365 дней для крипто) и Deflated Sharpe Ratio (Bailey & Lopez de Prado)
    по дневной кривой доходностей. При < 5 дней или нулевой дисперсии возвращает (None, None).
    """
    n = len(daily_returns)
    if n < 5:
        return None, None
    mean_r = sum(daily_returns) / n
    var_r = sum((r - mean_r) ** 2 for r in daily_returns) / max(n - 1, 1)
    if var_r <= 1e-18:
        return None, None
    std_r = math.sqrt(var_r)
    sr_daily = mean_r / std_r
    sharpe_ann = sr_daily * math.sqrt(365.0)

    # Моменты для DSR (skewness, kurtosis)
    m3 = sum(((r - mean_r) / std_r) ** 3 for r in daily_returns) / n
    m4 = sum(((r - mean_r) / std_r) ** 4 for r in daily_returns) / n
    # Масштабно-инвариантный ожидаемый максимальный дневной Sharpe при n_trials (Bailey & Lopez de Prado)
    if n_trials <= 1:
        sr_star = 0.0
    else:
        euler_gamma = 0.5772156649
        sr_trial_std = 1.0 / math.sqrt(max(n - 1, 1))
        sr_star = sr_trial_std * (
            (1.0 - euler_gamma) * _norm_ppf(1.0 - 1.0 / n_trials)
            + euler_gamma * _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        )
    denom_sq = max(1e-12, 1.0 - m3 * sr_daily + ((m4 - 1.0) / 4.0) * (sr_daily ** 2))
    z_dsr = ((sr_daily - sr_star) * math.sqrt(max(1, n - 1))) / math.sqrt(denom_sq)
    dsr = _norm_cdf(z_dsr)
    return round(sharpe_ann, 3), round(dsr, 4)


def run_monte_carlo_permutation(
    trade_pnls_usd: List[float],
    initial_equity: float = 1000.0,
    iterations: int = 5000,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Монте-Карло перестановка порядка сделок (без возвращения):
    оценивает распределение Max Drawdown (медиана, P95, P99) при случайном порядке сделок.
    """
    if len(trade_pnls_usd) < 3:
        return {
            "iterations": 0,
            "median_max_dd_pct": 0.0,
            "p95_max_dd_pct": 0.0,
            "p99_max_dd_pct": 0.0,
        }
    rng = random.Random(seed)
    buf = list(trade_pnls_usd)
    dds: List[float] = []
    for _ in range(max(100, iterations)):
        rng.shuffle(buf)
        eq = initial_equity
        peak = eq
        max_dd = 0.0
        for p in buf:
            eq += p
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = ((peak - eq) / peak) * 100.0
                if dd > max_dd:
                    max_dd = dd
        dds.append(max_dd)
    dds.sort()
    m = len(dds)
    return {
        "iterations": m,
        "median_max_dd_pct": round(dds[m // 2], 2),
        "p95_max_dd_pct": round(dds[min(m - 1, int(m * 0.95))], 2),
        "p99_max_dd_pct": round(dds[min(m - 1, int(m * 0.99))], 2),
    }


class WalkForwardBacktester:
    """Многомесячный исторический Replay и Rolling Walk-Forward бэктестер (Bybit V5)."""

    MIN_STOP_FRAC = MIN_STOP_FRAC

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or load_settings()
        self.client = BybitV5Client(self.settings, allow_offline_fallback=False)
        ex_cfg = self.settings.get("exchange", {})
        self.maker_fee = float(ex_cfg.get("maker_fee_rate", 0.0002))
        self.taker_fee = float(ex_cfg.get("taker_fee_rate", 0.00055))
        self.base_slippage = float(ex_cfg.get("backtest_slippage_rate", 0.0003))
        self.cache_dir: Path = BASE_DIR / "data" / "candles_cache"

    def load_historical_klines(
        self,
        symbol: str,
        interval: str = "15",
        days: int = 180,
        end_ms: Optional[int] = None,
    ) -> List[Candle]:
        """Загружает историю свечей с пагинацией и локальным кэшем (минимум 180 дней по умолчанию)."""
        klines = self.client.get_klines_paginated(
            symbol,
            interval=interval,
            days=days,
            end_ms=end_ms,
            cache_dir=self.cache_dir,
        )
        return klines[:-1] if len(klines) > 1 else klines

    def load_historical_funding(
        self,
        symbol: str,
        days: int = 180,
        end_ms: Optional[int] = None,
    ) -> List[Tuple[int, float]]:
        """Загружает историю ставок финансирования (8h) с пагинацией и локальным кэшем."""
        return self.client.get_funding_history_paginated(
            symbol,
            days=days,
            end_ms=end_ms,
            cache_dir=self.cache_dir,
        )

    def _resolve_trade(
        self,
        future_bars: List[Candle],
        side: str,
        entry_px: float,
        stop_loss: float,
        take_profit: float,
        entry_fee_frac: float,
        tp_fee_frac: float,
        sl_fee_frac: float,
        move_be_at_1r: bool,
        be_trigger_r: float = 1.0,
        trail_trigger_r: float = 1.5,
        trail_frac: float = 0.0085,
    ) -> Tuple[float, float, int]:
        risk_dist = abs(entry_px - stop_loss)
        if risk_dist <= 0 or not future_bars:
            return 0.0, 0.0, 1

        cur_sl = stop_loss
        be_armed = False
        be_px = entry_px * (1.0 + 0.0012) if side == "Buy" else entry_px * (1.0 - 0.0012)

        for idx, bar in enumerate(future_bars):
            if side == "Buy":
                hit_sl = bar.low <= cur_sl
                hit_tp = bar.high >= take_profit
                if hit_sl:
                    raw = (min(cur_sl, bar.open) - entry_px) / entry_px
                    net_pct = (raw - entry_fee_frac - sl_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if hit_tp:
                    raw = (take_profit - entry_px) / entry_px
                    net_pct = (raw - entry_fee_frac - tp_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if move_be_at_1r and not be_armed and bar.high >= entry_px + be_trigger_r * risk_dist:
                    cur_sl = max(cur_sl, be_px)
                    be_armed = True
                if move_be_at_1r and bar.high >= entry_px + trail_trigger_r * risk_dist:
                    cur_sl = max(cur_sl, bar.close * (1.0 - trail_frac))
            else:
                hit_sl = bar.high >= cur_sl
                hit_tp = bar.low <= take_profit
                if hit_sl:
                    raw = (entry_px - max(cur_sl, bar.open)) / entry_px
                    net_pct = (raw - entry_fee_frac - sl_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if hit_tp:
                    raw = (entry_px - take_profit) / entry_px
                    net_pct = (raw - entry_fee_frac - tp_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if move_be_at_1r and not be_armed and bar.low <= entry_px - be_trigger_r * risk_dist:
                    cur_sl = min(cur_sl, be_px)
                    be_armed = True
                if move_be_at_1r and bar.low <= entry_px - trail_trigger_r * risk_dist:
                    cur_sl = min(cur_sl, bar.close * (1.0 + trail_frac))

        last_close = future_bars[-1].close
        raw = ((last_close - entry_px) / entry_px) if side == "Buy" else ((entry_px - last_close) / entry_px)
        net_pct = (raw - entry_fee_frac - sl_fee_frac) * 100.0
        return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), len(future_bars)

    def _simulate_naive(
        self, candles: List[Candle], cost_multiplier: float = 1.0
    ) -> List[Tuple[int, float, float, str, int, str, float]]:
        trades: List[Tuple[int, float, float, str, int, str, float]] = []
        if len(candles) < 60:
            return trades
        atr_s = calc_atr_series(candles, 14)
        cost = (self.taker_fee + self.base_slippage) * cost_multiplier
        i = 24
        while i < len(candles) - 10:
            c_sig = candles[i - 1]
            c_prev = candles[i - 2]
            entry_px = candles[i].open
            atr = max(atr_s[i - 1], entry_px * 0.0015)
            side: Optional[str] = None
            if i % 3 == 0:
                side = "Buy" if c_sig.close < c_prev.close else "Sell"
            if side is None:
                i += 1
                continue
            stop_dist = max(0.75 * atr, entry_px * 0.0025)
            target_dist = 1.25 * atr
            sl = entry_px - stop_dist if side == "Buy" else entry_px + stop_dist
            tp = entry_px + target_dist if side == "Buy" else entry_px - target_dist
            net_pct, r_mult, used = self._resolve_trade(
                candles[i : i + 8],
                side,
                entry_px,
                sl,
                tp,
                cost,
                cost,
                cost,
                move_be_at_1r=False,
            )
            exit_ts = int(candles[min(i + used - 1, len(candles) - 1)].timestamp) + 900000
            trades.append((int(candles[i].timestamp), net_pct, r_mult, "NAIVE", exit_ts, "", stop_dist / entry_px))
            i += max(2, used)
        return trades

    def replay_signal(
        self,
        candles: List[Candle],
        i: int,
        sig: Any,
        cost_multiplier: float = 1.0,
    ) -> Optional[Tuple[int, float, float, str, int, str, float]]:
        """Shared realized label/replay; limit fill-bar profit is not assumed before fill."""
        hold = int(sig.features.get("max_hold_bars", 16))
        fill = i
        is_limit = str(getattr(sig, "order_type", "Market")) == "Limit"
        is_booster_trend = sig.strategy_id in ("S1_TREND_RETEST", "B1_SQUEEZE_BREAKOUT")
        if is_limit:
            fill = None
            ttl = int(sig.features.get("retest_ttl_bars", 3))
            for k in range(i, min(i + ttl, len(candles) - hold + 1)):
                if candles[k].low <= sig.entry_price <= candles[k].high:
                    fill = k
                    break
            if fill is None:
                return None
        if fill + hold > len(candles):
            return None
        bars = list(candles[fill : fill + hold])
        if is_limit:
            first = bars[0]
            bars[0] = replace(
                first,
                open=sig.entry_price,
                high=max(sig.entry_price, first.close) if sig.side == "Buy" else first.high,
                low=min(sig.entry_price, first.close) if sig.side == "Sell" else first.low,
            )
        is_post_only = bool(sig.features.get("post_only", False))
        entry_cost = (self.maker_fee if is_post_only else (self.taker_fee + self.base_slippage)) * cost_multiplier
        exit_cost = (self.taker_fee + self.base_slippage) * cost_multiplier
        # Pure SL/TP для B1/S1 (без раннего БУ/трейлинга, 1:1 с SmartPositionManager по данным MFE/MAE)
        net, r, used = self._resolve_trade(
            bars,
            sig.side,
            sig.entry_price,
            sig.stop_loss,
            sig.take_profit_2,
            entry_cost,
            exit_cost,
            exit_cost,
            False,
        )
        step_ms = (candles[1].timestamp - candles[0].timestamp) if len(candles) > 1 else 900000
        return (
            int(candles[fill].timestamp),
            net,
            r,
            sig.strategy_id,
            int(candles[fill + used - 1].timestamp) + step_ms,
            str(getattr(sig, "symbol", "")),
            abs(sig.entry_price - sig.stop_loss) / sig.entry_price,
        )

    def _simulate_s1_s3_replay_dual(
        self,
        candles: List[Candle],
        symbol: str = "REPLAY",
        strategy_fn: Optional[Callable[[str, List[Candle], float], Any]] = None,
        window_bars: int = 520,
    ) -> Tuple[List[Tuple[int, float, float, str, int, str, float]], List[Tuple[int, float, float, str, int, str, float]]]:
        trades_1x: List[Tuple[int, float, float, str, int, str, float]] = []
        trades_2x: List[Tuple[int, float, float, str, int, str, float]] = []
        fn = strategy_fn or (
            lambda sym, hist, ref_open: generate_s1_s3_signal(
                sym, hist, reference_open_px=ref_open, min_rr_s3=1.15
            )
        )
        i = 220
        n_bars = len(candles)
        while i < n_bars - 26:
            start_idx = max(0, i - window_bars)
            sig = fn(symbol, candles[start_idx:i], candles[i].open)
            if sig:
                t1 = self.replay_signal(candles, i, sig, cost_multiplier=1.0)
                if t1:
                    t2 = self.replay_signal(candles, i, sig, cost_multiplier=2.0)
                    trades_1x.append(t1)
                    if t2:
                        trades_2x.append(t2)
                    while i < n_bars and candles[i].timestamp < t1[4]:
                        i += 1
                    continue
            i += 1
        return trades_1x, trades_2x

    def _simulate_s1_s3_replay(
        self,
        candles: List[Candle],
        symbol: str = "REPLAY",
        cost_multiplier: float = 1.0,
        strategy_fn: Optional[Callable[[str, List[Candle], float], Any]] = None,
        window_bars: int = 520,
    ) -> List[Tuple[int, float, float, str, int, str, float]]:
        if abs(cost_multiplier - 1.0) < 1e-9:
            return self._simulate_s1_s3_replay_dual(candles, symbol=symbol, strategy_fn=strategy_fn, window_bars=window_bars)[0]
        trades: List[Tuple[int, float, float, str, int, str, float]] = []
        fn = strategy_fn or (
            lambda sym, hist, ref_open: generate_s1_s3_signal(
                sym, hist, reference_open_px=ref_open, min_rr_s3=1.15
            )
        )
        i = 220
        n_bars = len(candles)
        while i < n_bars - 26:
            start_idx = max(0, i - window_bars)
            sig = fn(symbol, candles[start_idx:i], candles[i].open)
            trade = self.replay_signal(candles, i, sig, cost_multiplier=cost_multiplier) if sig else None
            if trade:
                trades.append(trade)
                while i < n_bars and candles[i].timestamp < trade[4]:
                    i += 1
            else:
                i += 1
        return trades

    def _portfolio(
        self,
        trades: List[Tuple[Any, ...]],
        risk_per_trade_frac: float = 0.005,
        initial_equity: float = 1000.0,
        max_total_risk_frac: float = 0.02,
        max_leverage: float = 3.0,
        max_positions: int = 3,
        allow_compounding: bool = False,
    ) -> Tuple[List[float], float, float, int]:
        """Event-time cash accounting; reserve risk/notional until actual exit timestamp."""
        equity = initial_equity
        peak = equity
        dd = 0.0
        active: List[Tuple[int, float, float, float, str]] = []
        realized: List[float] = []
        skipped = 0
        self._portfolio_r: List[float] = []
        self._daily_pnl: Dict[int, float] = {}

        def settle(until: float) -> None:
            nonlocal equity, peak, dd, active
            due = sorted([x for x in active if x[0] <= until], key=lambda x: x[0])
            for end, pnl, risk, notional, symbol in due:
                equity += pnl
                realized.append(pnl)
                self._portfolio_r.append(pnl / risk if risk else 0.0)
                day_key = int(end // 86_400_000)
                self._daily_pnl[day_key] = self._daily_pnl.get(day_key, 0.0) + pnl
                peak = max(peak, equity)
                if peak > 0:
                    dd = max(dd, (peak - equity) / peak * 100.0)
            active = [x for x in active if x[0] > until]

        for index, t in enumerate(sorted(trades, key=lambda t: (t[0], t[5] if len(t) > 5 else ""))):
            settle(t[0])
            symbol = t[5] if len(t) > 5 and t[5] else str(index)
            stop = t[6] if len(t) > 6 else 0.0055
            cap_base = max(0.0, equity) if allow_compounding else min(initial_equity, max(0.0, equity))
            budget = cap_base * risk_per_trade_frac
            notional = budget / (stop + 0.0054)
            if (
                len(active) >= max_positions
                or any(x[4] == symbol for x in active)
                or sum(x[2] for x in active) + budget > cap_base * max_total_risk_frac + 1e-9
                or sum(x[3] for x in active) + notional > cap_base * max_leverage + 1e-9
            ):
                skipped += 1
                continue
            exit_ts = t[4] if len(t) > 4 else t[0] + 900000
            pnl = notional * t[1] / 100.0
            active.append((exit_ts, pnl, budget, notional, symbol))
        settle(float("inf"))
        return realized, equity, dd, skipped

    def _compute_daily_returns(self, initial_equity: float = 1000.0) -> List[float]:
        if not getattr(self, "_daily_pnl", None) or len(self._daily_pnl) < 2:
            return []
        days = sorted(self._daily_pnl.keys())
        first_d, last_d = days[0], days[-1]
        eq = initial_equity
        rets: List[float] = []
        for d in range(first_d, last_d + 1):
            pnl = self._daily_pnl.get(d, 0.0)
            if eq > 0:
                rets.append(pnl / eq)
            eq += pnl
        return rets

    def _summarize(
        self,
        mode_name: str,
        trades: List[Tuple[Any, ...]],
        n_trials: int = 1,
        risk_per_trade_frac: float = 0.005,
        max_total_risk_frac: float = 0.02,
        max_leverage: float = 3.0,
        max_positions: int = 3,
        allow_compounding: bool = False,
    ) -> BacktestSummary:
        if not trades:
            return BacktestSummary(mode_name, 0, 0, 0, 0.0, None, 0.0, 0.0, 0.0, None, None)

        pnls, equity, max_dd, _ = self._portfolio(
            trades,
            risk_per_trade_frac=risk_per_trade_frac,
            max_total_risk_frac=max_total_risk_frac,
            max_leverage=max_leverage,
            max_positions=max_positions,
            allow_compounding=allow_compounding,
        )
        if not pnls:
            return BacktestSummary(mode_name, 0, 0, 0, 0.0, None, 0.0, 0.0, 0.0, None, None)
        r_mults = list(self._portfolio_r)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / len(pnls) * 100.0
        profit_factor = (sum(wins) / abs(sum(losses))) if sum(losses) < 0 else None
        net_ret = (equity / 1000.0 - 1.0) * 100.0
        mean_r = sum(r_mults) / len(r_mults)
        daily_rets = self._compute_daily_returns(1000.0)
        sharpe, dsr = compute_daily_sharpe_and_dsr(daily_rets, n_trials=n_trials)

        return BacktestSummary(
            mode_name=mode_name,
            total_trades=len(pnls),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=round(win_rate, 2),
            profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
            net_return_pct=round(net_ret, 2),
            avg_r_multiple=round(mean_r, 3),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=sharpe,
            deflated_sharpe_ratio=dsr,
        )

    def _evaluate_rolling_wfa_from_trades(
        self,
        all_1x: List[Tuple[int, float, float, str, int, str, float]],
        all_2x: List[Tuple[int, float, float, str, int, str, float]],
        min_ts: Optional[int],
        max_ts: Optional[int],
        n_cycles: int = 6,
        is_ratio: float = 0.75,
        n_trials: int = 1,
        mc_iterations: int = 5000,
        risk_per_trade_frac: float = 0.005,
        max_total_risk_frac: float = 0.02,
        max_leverage: float = 3.0,
        max_positions: int = 3,
        allow_compounding: bool = False,
    ) -> Dict[str, Any]:
        if min_ts is None or max_ts is None or max_ts <= min_ts:
            return {
                "status": "insufficient_data",
                "n_cycles": n_cycles,
                "is_ratio": is_ratio,
                "n_trials": n_trials,
                "cycles": [],
                "wfe": None,
                "wfe_passed": False,
                "oos_summary": asdict(self._summarize("OOS (1x)", [])),
                "oos_stress_2x": asdict(self._summarize("OOS (2x Stress)", [])),
                "monte_carlo": run_monte_carlo_permutation([]),
            }

        total_span = max_ts - min_ts
        oos_ratio = max(0.05, min(0.50, 1.0 - is_ratio))
        window_span = total_span / (1.0 + max(0, n_cycles - 1) * oos_ratio)
        is_span = window_span * is_ratio
        oos_span = window_span * oos_ratio

        cycles_out: List[Dict[str, Any]] = []
        oos_trades_1x: List[Tuple[int, float, float, str, int, str, float]] = []
        oos_trades_2x: List[Tuple[int, float, float, str, int, str, float]] = []
        ann_is_list: List[float] = []
        ann_oos_list: List[float] = []

        is_days = max(1.0, is_span / 86_400_000.0)
        oos_days = max(1.0, oos_span / 86_400_000.0)
        p_kw = dict(
            risk_per_trade_frac=risk_per_trade_frac,
            max_total_risk_frac=max_total_risk_frac,
            max_leverage=max_leverage,
            max_positions=max_positions,
            allow_compounding=allow_compounding,
        )

        for c in range(n_cycles):
            w_start = int(min_ts + c * oos_span)
            is_end = int(w_start + is_span)
            oos_end = int(min(max_ts + 1, is_end + oos_span))

            c_is = [t for t in all_1x if w_start <= t[0] < is_end]
            c_oos = [t for t in all_1x if is_end <= t[0] < oos_end]
            c_oos_2x = [t for t in all_2x if is_end <= t[0] < oos_end]

            s_is = self._summarize(f"Cycle {c + 1} IS", c_is, n_trials=n_trials, **p_kw)
            s_oos = self._summarize(f"Cycle {c + 1} OOS", c_oos, n_trials=n_trials, **p_kw)
            ann_is = s_is.net_return_pct * (365.0 / is_days)
            ann_oos = s_oos.net_return_pct * (365.0 / oos_days)
            ann_is_list.append(ann_is)
            ann_oos_list.append(ann_oos)
            oos_trades_1x.extend(c_oos)
            oos_trades_2x.extend(c_oos_2x)

            cycles_out.append({
                "cycle": c + 1,
                "is_start_ms": w_start,
                "is_end_ms": is_end,
                "oos_end_ms": oos_end,
                "is_trades": s_is.total_trades,
                "oos_trades": s_oos.total_trades,
                "is_return_pct": s_is.net_return_pct,
                "oos_return_pct": s_oos.net_return_pct,
                "oos_win_rate_pct": s_oos.win_rate_pct,
                "oos_pf": s_oos.profit_factor,
                "oos_avg_r": s_oos.avg_r_multiple,
            })

        mean_ann_is = sum(ann_is_list) / len(ann_is_list) if ann_is_list else 0.0
        mean_ann_oos = sum(ann_oos_list) / len(ann_oos_list) if ann_oos_list else 0.0
        wfe = round(mean_ann_oos / mean_ann_is, 3) if (mean_ann_is > 1e-6) else None

        oos_summary = self._summarize("Rolling WFA OOS (1x)", oos_trades_1x, n_trials=n_trials, **p_kw)
        oos_pnls, _, _, _ = self._portfolio(oos_trades_1x, **p_kw)
        oos_stress_2x = self._summarize("Rolling WFA OOS (2x Stress)", oos_trades_2x, n_trials=n_trials, **p_kw)
        mc = run_monte_carlo_permutation(oos_pnls, initial_equity=1000.0, iterations=mc_iterations)
        wfe_ok = bool(
            wfe is not None
            and wfe >= 0.50
            and oos_summary.net_return_pct > 0.0
            and oos_summary.avg_r_multiple > 0.0
            and oos_stress_2x.avg_r_multiple > 0.0
        )

        return {
            "status": "ok",
            "n_cycles": n_cycles,
            "is_ratio": is_ratio,
            "n_trials": n_trials,
            "wfe": wfe,
            "wfe_passed": wfe_ok,
            "cycles": cycles_out,
            "oos_summary": asdict(oos_summary),
            "oos_stress_2x": asdict(oos_stress_2x),
            "monte_carlo": mc,
        }

    def run_rolling_wfa(
        self,
        candles_by_symbol: Dict[str, List[Candle]],
        strategy_fn: Optional[Callable[[str, List[Candle], float], Any]] = None,
        n_cycles: int = 6,
        is_ratio: float = 0.75,
        n_trials: int = 1,
        mc_iterations: int = 5000,
        risk_per_trade_frac: float = 0.005,
        max_total_risk_frac: float = 0.02,
        max_leverage: float = 3.0,
        max_positions: int = 3,
        allow_compounding: bool = False,
    ) -> Dict[str, Any]:
        """
        Скользящий Walk-Forward (Rolling WFA): минимум `n_cycles` циклов (75% IS / 25% OOS),
        расчёт WFE, честного Sharpe/DSR, стресс-теста 2x издержек и Монте-Карло перестановки.
        """
        all_1x: List[Tuple[int, float, float, str, int, str, float]] = []
        all_2x: List[Tuple[int, float, float, str, int, str, float]] = []
        min_ts: Optional[int] = None
        max_ts: Optional[int] = None

        for sym, bars in candles_by_symbol.items():
            if len(bars) < 260:
                continue
            min_ts = bars[0].timestamp if min_ts is None else min(min_ts, bars[0].timestamp)
            max_ts = bars[-1].timestamp if max_ts is None else max(max_ts, bars[-1].timestamp)
            t1x, t2x = self._simulate_s1_s3_replay_dual(bars, symbol=sym, strategy_fn=strategy_fn)
            all_1x.extend(t1x)
            all_2x.extend(t2x)

        return self._evaluate_rolling_wfa_from_trades(
            all_1x,
            all_2x,
            min_ts,
            max_ts,
            n_cycles=n_cycles,
            is_ratio=is_ratio,
            n_trials=n_trials,
            mc_iterations=mc_iterations,
            risk_per_trade_frac=risk_per_trade_frac,
            max_total_risk_frac=max_total_risk_frac,
            max_leverage=max_leverage,
            max_positions=max_positions,
            allow_compounding=allow_compounding,
        )

    def run_momentum_funding_wfa(
        self,
        candles_by_symbol: Dict[str, List[Candle]],
        funding_history_by_symbol: Optional[Dict[str, List[Tuple[int, float]]]] = None,
        n_cycles: int = 6,
        is_ratio: float = 0.75,
        n_trials: int = 4,
        mc_iterations: int = 2000,
        risk_per_trade_frac: float = 0.01,
        max_total_risk_frac: float = 0.025,
        max_leverage: float = 3.0,
        max_positions: int = 3,
        allow_compounding: bool = False,
        liquid_window_only: bool = True,
        rebalance_step_hours: int = 4,
        top_n: int = 1,
        min_mom_threshold: float = 0.8,
        hold_bars_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Честный Rolling WFA для кросс-секционной стратегии AUTOTRADE_MOMENTUM_FUNDING:
        - Выравнивание 1h баров строго по общему `timestamp` (без смещения индексов).
        - Point-in-time подстановка исторической ставки фандинга (`ts <= bar_ts`) без заглядывания в будущее.
        - Опциональная фильтрация по ликвидному окну Автотрейдинга (07:00–20:00 UTC).
        - 6-цикловой Rolling WFA (WFE, 1x/2x Stress, Sharpe, DSR, MC P95 DD).
        """
        from bisect import bisect_right
        from aitrade.engine import is_in_autotrade_liquid_window
        from aitrade.strategies.momentum_funding import generate_momentum_funding_signals
        from aitrade.strategies.regime_s1_s3 import aggregate_1h

        h1_map: Dict[str, List[Candle]] = {}
        for sym, bars in candles_by_symbol.items():
            if len(bars) < 40:
                continue
            step_ms = (bars[1].timestamp - bars[0].timestamp) if len(bars) > 1 else 900_000
            h1_bars = bars if step_ms >= 3_600_000 else aggregate_1h(bars)
            if len(h1_bars) >= 40:
                h1_map[sym] = h1_bars

        if len(h1_map) < 2:
            return self._evaluate_rolling_wfa_from_trades([], [], None, None, n_cycles=n_cycles, is_ratio=is_ratio, n_trials=n_trials, mc_iterations=mc_iterations)

        ts_to_idx: Dict[str, Dict[int, int]] = {
            sym: {int(b.timestamp): i for i, b in enumerate(bars)} for sym, bars in h1_map.items()
        }
        common_ts = sorted(set.intersection(*(set(m.keys()) for m in ts_to_idx.values())))
        hold_h = int(hold_bars_override if hold_bars_override is not None else 8)
        if len(common_ts) < 40 + hold_h:
            return self._evaluate_rolling_wfa_from_trades([], [], None, None, n_cycles=n_cycles, is_ratio=is_ratio, n_trials=n_trials, mc_iterations=mc_iterations)

        fr_hist = funding_history_by_symbol or {}
        fr_ts_map: Dict[str, List[int]] = {}
        fr_val_map: Dict[str, List[float]] = {}
        for sym in h1_map:
            pts = sorted(fr_hist.get(sym, []), key=lambda x: int(x[0]))
            fr_ts_map[sym] = [int(p[0]) for p in pts]
            fr_val_map[sym] = [float(p[1]) for p in pts]

        def _pit_funding(sym: str, ts_ms: int) -> float:
            ts_arr = fr_ts_map.get(sym)
            if not ts_arr:
                return 0.0
            pos = bisect_right(ts_arr, ts_ms) - 1
            return fr_val_map[sym][pos] if pos >= 0 else 0.0

        all_1x: List[Tuple[int, float, float, str, int, str, float]] = []
        all_2x: List[Tuple[int, float, float, str, int, str, float]] = []
        next_free_ts: Dict[str, int] = {}
        step = max(1, int(rebalance_step_hours))

        for k in range(30, len(common_ts) - hold_h - 2, step):
            sig_ts = common_ts[k]
            if liquid_window_only and not is_in_autotrade_liquid_window(sig_ts, self.settings):
                continue
            snap_1h: Dict[str, List[Candle]] = {}
            pit_fr: Dict[str, float] = {}
            for sym, bars in h1_map.items():
                idx = ts_to_idx[sym][sig_ts]
                if idx < 30:
                    continue
                snap_1h[sym] = bars[max(0, idx - 120) : idx]
                pit_fr[sym] = _pit_funding(sym, sig_ts)
            if len(snap_1h) < 2:
                continue
            sigs = generate_momentum_funding_signals(
                snap_1h, pit_fr, top_n=top_n, min_mom_threshold=min_mom_threshold
            )
            for sig in sigs:
                if sig_ts < next_free_ts.get(sig.symbol, 0):
                    continue
                if hold_bars_override is not None:
                    sig.features["max_hold_bars"] = float(hold_bars_override)
                sym_idx = ts_to_idx[sig.symbol][sig_ts]
                t1 = self.replay_signal(h1_map[sig.symbol], sym_idx, sig, cost_multiplier=1.0)
                if t1:
                    t2 = self.replay_signal(h1_map[sig.symbol], sym_idx, sig, cost_multiplier=2.0)
                    all_1x.append(t1)
                    if t2:
                        all_2x.append(t2)
                    next_free_ts[sig.symbol] = int(t1[4])

        return self._evaluate_rolling_wfa_from_trades(
            all_1x,
            all_2x,
            common_ts[0],
            common_ts[-1],
            n_cycles=n_cycles,
            is_ratio=is_ratio,
            n_trials=n_trials,
            mc_iterations=mc_iterations,
            risk_per_trade_frac=risk_per_trade_frac,
            max_total_risk_frac=max_total_risk_frac,
            max_leverage=max_leverage,
            max_positions=max_positions,
            allow_compounding=allow_compounding,
        )

    def run_multi_strategy_wfa(
        self,
        candles_by_symbol: Dict[str, List[Candle]],
        funding_rates: Optional[Dict[str, float]] = None,
        funding_history_by_symbol: Optional[Dict[str, List[Tuple[int, float]]]] = None,
        n_cycles: int = 6,
        mc_iterations: int = 2000,
    ) -> Dict[str, Any]:
        """
        Прогоняет все направленные стратегии Этапа 2 через Rolling WFA:
        - B1_SQUEEZE_BREAKOUT (Разгон)
        - S1_TREND_RETEST (Разгон B2)
        - S3_VWAP_RANGE (Автотрейдинг Рукав A)
        - AUTOTRADE_MOMENTUM_FUNDING (Автотрейдинг Рукав B)
        """
        from bisect import bisect_right
        from aitrade.strategies.booster_breakout import generate_booster_breakout_signal
        from aitrade.strategies.regime_s1_s3 import aggregate_1h

        fr_map = funding_rates or {}
        h1_map = {sym: aggregate_1h(bars) for sym, bars in candles_by_symbol.items() if len(bars) >= 260}
        h1_ts_map = {sym: [int(b.timestamp) for b in bars] for sym, bars in h1_map.items()}

        def _h1_slice(sym: str, hist: List[Candle]) -> Optional[List[Candle]]:
            h1_all = h1_map.get(sym)
            if not h1_all or not hist:
                return None
            cutoff = int(hist[-1].timestamp) - 2_700_000
            pos = bisect_right(h1_ts_map[sym], cutoff)
            if pos < 55:
                return None
            return h1_all[max(0, pos - 125) : pos]

        def fn_b1(sym: str, hist: List[Candle], ref_open: float):
            return generate_booster_breakout_signal(
                sym, hist, reference_open_px=ref_open, bars_1h=_h1_slice(sym, hist), funding_rate=fr_map.get(sym, 0.0)
            )

        def fn_s1(sym: str, hist: List[Candle], ref_open: float):
            sig = generate_s1_s3_signal(
                sym, hist, reference_open_px=ref_open, bars_1h=_h1_slice(sym, hist), min_rr_s3=1.15
            )
            return sig if (sig and sig.strategy_id == "S1_TREND_RETEST") else None

        def fn_s3(sym: str, hist: List[Candle], ref_open: float):
            sig = generate_s1_s3_signal(
                sym, hist, reference_open_px=ref_open, bars_1h=_h1_slice(sym, hist), min_rr_s3=1.15
            )
            return sig if (sig and sig.strategy_id == "S3_VWAP_RANGE") else None

        fr_hist = funding_history_by_symbol
        if fr_hist is None and fr_map:
            fr_hist = {sym: [(0, float(rate))] for sym, rate in fr_map.items()}

        return {
            "B1_SQUEEZE_BREAKOUT": self.run_rolling_wfa(candles_by_symbol, strategy_fn=fn_b1, n_cycles=n_cycles, n_trials=4, mc_iterations=mc_iterations, risk_per_trade_frac=0.04, max_total_risk_frac=0.07, max_leverage=5.0, max_positions=2, allow_compounding=True),
            "S1_TREND_RETEST": self.run_rolling_wfa(candles_by_symbol, strategy_fn=fn_s1, n_cycles=n_cycles, n_trials=4, mc_iterations=mc_iterations, risk_per_trade_frac=0.04, max_total_risk_frac=0.07, max_leverage=5.0, max_positions=2, allow_compounding=True),
            "S3_VWAP_RANGE": self.run_rolling_wfa(candles_by_symbol, strategy_fn=fn_s3, n_cycles=n_cycles, n_trials=4, mc_iterations=mc_iterations, risk_per_trade_frac=0.01, max_total_risk_frac=0.025, max_leverage=3.0, max_positions=3, allow_compounding=False),
            "AUTOTRADE_MOMENTUM_FUNDING": self.run_momentum_funding_wfa(
                candles_by_symbol,
                funding_history_by_symbol=fr_hist,
                n_cycles=n_cycles,
                n_trials=4,
                mc_iterations=mc_iterations,
                risk_per_trade_frac=0.01,
                max_total_risk_frac=0.025,
                max_leverage=3.0,
                max_positions=3,
                allow_compounding=False,
            ),
        }

    def run_comparison_backtest(
        self,
        symbols: Optional[List[str]] = None,
        days: int = 180,
        n_cycles: int = 6,
    ) -> Dict[str, Any]:
        pairs = symbols or self.settings["universe"]["core_pairs"][:6]
        naive_trades: List[Tuple[int, float, float, str, int, str, float]] = []
        sniper_trades: List[Tuple[int, float, float, str, int, str, float]] = []
        candles_by_symbol: Dict[str, List[Candle]] = {}

        provenance = []
        for sym in pairs:
            closed_klines = self.load_historical_klines(sym, interval="15", days=days)
            if not closed_klines:
                continue
            candles_by_symbol[sym] = closed_klines
            raw = [asdict(c) for c in closed_klines]
            digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
            archive = BASE_DIR / "data" / "research_bars" / f"{sym}-{digest[:16]}.json"
            if not archive.exists():
                atomic_json(archive, raw)
            provenance.append({
                "symbol": sym,
                "sha256": digest,
                "bars": len(raw),
                "source": self.client.last_data_source,
                "first_ms": closed_klines[0].timestamp,
                "last_ms": closed_klines[-1].timestamp,
            })
            naive_trades.extend(tuple(list(t[:5]) + [sym, t[6]]) for t in self._simulate_naive(closed_klines))
            sniper_trades.extend(self._simulate_s1_s3_replay(closed_klines, symbol=sym))

        naive_summary = self._summarize("Наивный режим (Taker без фильтра режима)", naive_trades)
        sniper_summary = self._summarize("Rolling Replay S1 + S3 (1x издержки)", sniper_trades)
        s1_trades = [t for t in sniper_trades if t[3] == "S1_TREND_RETEST"]
        s3_trades = [t for t in sniper_trades if t[3] == "S3_VWAP_RANGE"]
        wfa_res = self.run_rolling_wfa(candles_by_symbol, n_cycles=n_cycles, is_ratio=0.75, n_trials=2, mc_iterations=5000)

        return {
            "validation_status": "rolling_wfa_research",
            "days_requested": days,
            "limitations": [
                "Нет исторического L2 и очереди PostOnly лимитов",
                "News/ML/correlation gates оцениваются отдельно от базового replay",
            ],
            "provenance": provenance,
            "portfolio_skipped": self._portfolio(sniper_trades)[3],
            "data_source": self.client.last_data_source,
            "pairs_tested": pairs,
            "naive_baseline": asdict(naive_summary),
            "aitrade_sniper_combine": asdict(sniper_summary),
            "rolling_wfa": wfa_res,
            "strategy_breakdown": {
                "S1_TREND_RETEST": asdict(self._summarize("S1: Пробой с ретестом (1h Trend)", s1_trades)),
                "S3_VWAP_RANGE": asdict(self._summarize("S3: Возврат к VWAP ±2σ (1h Range)", s3_trades)),
            },
        }

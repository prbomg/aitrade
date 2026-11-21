#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Исследовательский пакет intraday.16 (180 дней, 12 кэшированных пар Bybit V5):
1. MR1_EXTREME_FLUSH (1h контр-тренд после экстремального выноса) — 6-цикловой Rolling WFA.
2. T1_DONCHIAN_TSMOM (4h Vol-Target) — проверка чувствительности целей TP2 (3R / 4R / 6R / 8R) и горизонта (5d / 7d / 10d).

Запуск на VPS:
    cd /opt/AItrade && sudo -u aitrade python3 -u scripts/run_research_intraday16.py
"""

from __future__ import annotations

import argparse
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.config import BASE_DIR, load_settings
from aitrade.storage import atomic_json
from aitrade.strategies.donchian_tsmom import aggregate_4h, generate_donchian_tsmom_signal
from aitrade.strategies.extreme_flush_mr import generate_extreme_flush_mr_signal
from aitrade.strategies.regime_s1_s3 import aggregate_1h


def _fmt_row(name: str, v: Dict[str, Any], elapsed_s: float) -> str:
    o1, o2, mc = v["oos_summary"], v["oos_stress_2x"], v["monte_carlo"]
    return (
        f"{name:34s} | N={o1['total_trades']:3d} | WR={o1['win_rate_pct']:5.1f}% | PF={str(o1['profit_factor']):4s} | "
        f"Net={o1['net_return_pct']:+6.1f}% | 1xR={o1['avg_r_multiple']:+.3f} | 2xR={o2['avg_r_multiple']:+.3f} | "
        f"WFE={str(v.get('wfe')):5s}({str(v.get('wfe_passed')):5s}) | SR={str(o1['sharpe_ratio']):5s} | "
        f"DSR={str(o1['deflated_sharpe_ratio']):6s} | DD={o1['max_drawdown_pct']:5.1f}% | MC95={mc['p95_max_dd_pct']:5.1f}% | {elapsed_s:.1f}s"
    )


def _run_mr1_wfa(
    bt: WalkForwardBacktester,
    h1_by_sym: Dict[str, List[Any]],
    fr_ts: Dict[str, List[int]],
    fr_val: Dict[str, List[float]],
    z_thr: float,
    tp_rr: float,
    max_hold: int,
    require_wick: bool,
    n_cycles: int,
    mc_iterations: int,
) -> Dict[str, Any]:
    all_1x: List[Tuple[Any, ...]] = []
    all_2x: List[Tuple[Any, ...]] = []
    min_ts = None
    max_ts = None
    for sym, bars in h1_by_sym.items():
        if len(bars) < 60 + max_hold:
            continue
        min_ts = bars[0].timestamp if min_ts is None else min(min_ts, bars[0].timestamp)
        max_ts = bars[-1].timestamp if max_ts is None else max(max_ts, bars[-1].timestamp)
        i = 36
        n = len(bars)
        arr = fr_ts.get(sym, [])
        vals = fr_val.get(sym, [])
        while i < n - max_hold - 1:
            hist = bars[max(0, i - 48) : i]
            pos = bisect_right(arr, int(hist[-1].timestamp)) - 1 if arr else -1
            fr = vals[pos] if pos >= 0 else 0.0
            sig = generate_extreme_flush_mr_signal(
                sym,
                hist,
                reference_open_px=bars[i].open,
                funding_rate=fr,
                z_threshold=z_thr,
                tp_rr=tp_rr,
                max_hold_bars=max_hold,
                require_wick=require_wick,
            )
            if sig is not None:
                t1 = bt.replay_signal(bars, i, sig, cost_multiplier=1.0)
                if t1 is not None:
                    t2 = bt.replay_signal(bars, i, sig, cost_multiplier=2.0)
                    all_1x.append(t1)
                    if t2 is not None:
                        all_2x.append(t2)
                    while i < n and bars[i].timestamp < t1[4]:
                        i += 1
                    continue
            i += 1
    return bt._evaluate_rolling_wfa_from_trades(
        all_1x,
        all_2x,
        min_ts,
        max_ts,
        n_cycles=n_cycles,
        n_trials=4,
        mc_iterations=mc_iterations,
        risk_per_trade_frac=0.015,
        max_total_risk_frac=0.06,
        max_leverage=3.0,
        max_positions=4,
        allow_compounding=False,
    )


def _run_t1_tp_grid(
    bt: WalkForwardBacktester,
    h4_by_sym: Dict[str, List[Any]],
    fr_ts: Dict[str, List[int]],
    fr_val: Dict[str, List[float]],
    tp_rr: float,
    max_hold: int,
    n_cycles: int,
    mc_iterations: int,
) -> Dict[str, Any]:
    all_1x: List[Tuple[Any, ...]] = []
    all_2x: List[Tuple[Any, ...]] = []
    min_ts = None
    max_ts = None
    for sym, bars in h4_by_sym.items():
        if len(bars) < 95 + max_hold:
            continue
        min_ts = bars[0].timestamp if min_ts is None else min(min_ts, bars[0].timestamp)
        max_ts = bars[-1].timestamp if max_ts is None else max(max_ts, bars[-1].timestamp)
        i = 90
        n = len(bars)
        arr = fr_ts.get(sym, [])
        vals = fr_val.get(sym, [])
        while i < n - max_hold - 1:
            hist = bars[max(0, i - 160) : i]
            pos = bisect_right(arr, int(hist[-1].timestamp)) - 1 if arr else -1
            fr = vals[pos] if pos >= 0 else 0.0
            sig = generate_donchian_tsmom_signal(
                sym, hist, reference_open_px=bars[i].open, funding_rate=fr, horizons=(20, 40, 80), max_hold_bars=max_hold
            )
            if sig is not None:
                sl_dist = abs(sig.entry_price - sig.stop_loss)
                sig.take_profit_2 = sig.entry_price + tp_rr * sl_dist if sig.side == "Buy" else sig.entry_price - tp_rr * sl_dist
                t1 = bt.replay_signal(bars, i, sig, cost_multiplier=1.0)
                if t1 is not None:
                    t2 = bt.replay_signal(bars, i, sig, cost_multiplier=2.0)
                    mult = float(sig.features.get("vol_scale", 1.0))
                    all_1x.append(tuple(list(t1) + [mult]))
                    if t2 is not None:
                        all_2x.append(tuple(list(t2) + [mult]))
                    while i < n and bars[i].timestamp < t1[4]:
                        i += 1
                    continue
            i += 1
    return bt._evaluate_rolling_wfa_from_trades(
        all_1x,
        all_2x,
        min_ts,
        max_ts,
        n_cycles=n_cycles,
        n_trials=4,
        mc_iterations=mc_iterations,
        risk_per_trade_frac=0.015,
        max_total_risk_frac=0.06,
        max_leverage=3.0,
        max_positions=4,
        allow_compounding=False,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--mc", type=int, default=2000)
    args = ap.parse_args()

    settings = load_settings()
    symbols = list(settings["universe"]["autotrade_pairs"][:12])
    bt = WalkForwardBacktester(settings)

    h1_by_sym: Dict[str, List[Any]] = {}
    h4_by_sym: Dict[str, List[Any]] = {}
    fr_ts: Dict[str, List[int]] = {}
    fr_val: Dict[str, List[float]] = {}

    for s in symbols:
        c15 = bt.load_historical_klines(s, interval="15", days=args.days)
        fh = bt.load_historical_funding(s, days=args.days)
        h1_by_sym[s] = aggregate_1h(c15)
        h4_by_sym[s] = aggregate_4h(c15)
        fr_ts[s] = [p[0] for p in fh]
        fr_val[s] = [p[1] for p in fh]
    print(f"[DATA] Загружено из кэша {len(symbols)} пар ({args.days}д)", flush=True)

    out: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
        "mr1_extreme_flush": {},
        "t1_tp_sensitivity": {},
    }

    print(f"\n[1/2] MR1_EXTREME_FLUSH (1h контр-тренд после выноса, 12 пар, {args.cycles}-цикловой WFA):", flush=True)
    for label, zt, rr, mh, rw in [
        ("MR1 Z>=2.2, TP=2.0R, 12h, Wick", 2.2, 2.0, 12, True),
        ("MR1 Z>=2.4, TP=2.2R, 12h, Wick", 2.4, 2.2, 12, True),
        ("MR1 Z>=2.6, TP=2.5R, 18h, Wick", 2.6, 2.5, 18, True),
        ("MR1 Z>=2.4, TP=2.2R, 18h, NoWick", 2.4, 2.2, 18, False),
    ]:
        t0 = time.time()
        v = _run_mr1_wfa(bt, h1_by_sym, fr_ts, fr_val, zt, rr, mh, rw, args.cycles, args.mc)
        print(_fmt_row(label, v, time.time() - t0), flush=True)
        out["mr1_extreme_flush"][label] = {"wfe": v.get("wfe"), "wfe_passed": v.get("wfe_passed"), "oos": v["oos_summary"], "oos_2x": v["oos_stress_2x"], "mc": v["monte_carlo"]}

    print(f"\n[2/2] T1_DONCHIAN_TSMOM (4h Vol-Target) — чувствительность TP2 и удержания (12 пар):", flush=True)
    for label, rr, mh in [
        ("T1 Vol TP=3.0R, Hold=5d  (30b)", 3.0, 30),
        ("T1 Vol TP=4.0R, Hold=5d  (base)", 4.0, 30),
        ("T1 Vol TP=6.0R, Hold=7d  (42b)", 6.0, 42),
        ("T1 Vol TP=8.0R, Hold=10d (60b)", 8.0, 60),
    ]:
        t0 = time.time()
        v = _run_t1_tp_grid(bt, h4_by_sym, fr_ts, fr_val, rr, mh, args.cycles, args.mc)
        print(_fmt_row(label, v, time.time() - t0), flush=True)
        out["t1_tp_sensitivity"][label] = {"wfe": v.get("wfe"), "wfe_passed": v.get("wfe_passed"), "oos": v["oos_summary"], "oos_2x": v["oos_stress_2x"], "mc": v["monte_carlo"]}

    out_path = BASE_DIR / "data" / "research_intraday16.json"
    atomic_json(out_path, out)
    print(f"\n[SAVED] {out_path}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Исследовательский пакет intraday.12 (180 дней, 12 ликвидных пар Bybit V5):
1. T1_DONCHIAN_TSMOM (4h Donchian Ensemble + Volatility Targeting) — 6-цикловой Rolling WFA.
2. C1_FUNDING_CARRY (дельта-нейтральный Spot + Perp) — 180-дневный бэктест на реальной истории фандинга (1x и 2x Maker).
3. B1_SQUEEZE_BREAKOUT на расширенном пуле 12 пар: базовый vs фильтр выходных (без Sat 18:00–Sun 22:00 UTC) vs консервативный риск для Автотрейдинга.

Запуск на VPS:
    cd /opt/AItrade && sudo -u aitrade python3 -u scripts/run_research_intraday12.py
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
from aitrade.strategies.booster_breakout import generate_booster_breakout_signal
from aitrade.strategies.regime_s1_s3 import aggregate_1h


def _is_weekend_chop_utc(ts_ms: int) -> bool:
    """Суббота 18:00 UTC — Воскресенье 22:00 UTC (до открытия Monday Asia Open)."""
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    wd = dt.weekday()  # 5 = Sat, 6 = Sun
    if wd == 5 and dt.hour >= 18:
        return True
    if wd == 6 and dt.hour < 22:
        return True
    return False


def _fmt_wfa_row(name: str, v: Dict[str, Any], elapsed_s: float) -> str:
    o1, o2, mc = v["oos_summary"], v["oos_stress_2x"], v["monte_carlo"]
    return (
        f"{name:32s} | N={o1['total_trades']:3d} | WR={o1['win_rate_pct']:5.1f}% | PF={str(o1['profit_factor']):4s} | "
        f"Net={o1['net_return_pct']:+6.1f}% | 1xR={o1['avg_r_multiple']:+.3f} | 2xR={o2['avg_r_multiple']:+.3f} | "
        f"WFE={str(v.get('wfe')):5s}({str(v.get('wfe_passed')):5s}) | SR={str(o1['sharpe_ratio']):5s} | "
        f"DSR={str(o1['deflated_sharpe_ratio']):6s} | DD={o1['max_drawdown_pct']:5.1f}% | MC95={mc['p95_max_dd_pct']:5.1f}% | {elapsed_s:.1f}s"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--mc", type=int, default=2000)
    ap.add_argument("--pairs", type=int, default=12)
    args = ap.parse_args()

    settings = load_settings()
    symbols = list(settings["universe"]["autotrade_pairs"][: args.pairs])
    bt = WalkForwardBacktester(settings)

    candles: Dict[str, List[Any]] = {}
    funding_hist: Dict[str, List[Tuple[int, float]]] = {}
    for s in symbols:
        t0 = time.time()
        candles[s] = bt.load_historical_klines(s, interval="15", days=args.days)
        t1 = time.time()
        funding_hist[s] = bt.load_historical_funding(s, days=args.days)
        print(
            f"[DATA] {s:8s}: {len(candles[s]):5d} bars ({t1 - t0:.1f}s) | {len(funding_hist[s]):3d} funding ({time.time() - t1:.1f}s)",
            flush=True,
        )

    results: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "symbols": symbols,
        "t1_donchian_tsmom": {},
        "c1_funding_carry": {},
        "b1_expanded_12p": {},
    }

    # 1) T1_DONCHIAN_TSMOM (4h, 12 пар)
    print(f"\n[1/3] T1_DONCHIAN_TSMOM — {args.cycles}-цикловой Rolling WFA ({args.days}д, {len(symbols)} пар):", flush=True)
    for label, horizons, max_hold, use_vol in [
        ("T1 Donchian (20/40/80, 5d, Fix)", (20, 40, 80), 30, False),
        ("T1 Donchian (20/40/80, 5d, Vol)", (20, 40, 80), 30, True),
        ("T1 Donchian (12/24/48, 3d, Fix)", (12, 24, 48), 18, False),
        ("T1 Donchian (12/24/48, 3d, Vol)", (12, 24, 48), 18, True),
    ]:
        t0 = time.time()
        v = bt.run_donchian_tsmom_wfa(
            candles,
            funding_history_by_symbol=funding_hist,
            n_cycles=args.cycles,
            n_trials=4,
            mc_iterations=args.mc,
            horizons=horizons,
            max_hold_bars=max_hold,
            use_vol_targeting=use_vol,
            risk_per_trade_frac=0.015,
            max_total_risk_frac=0.06,
            max_leverage=3.0,
            max_positions=4,
            allow_compounding=False,
        )
        print(_fmt_wfa_row(label, v, time.time() - t0), flush=True)
        results["t1_donchian_tsmom"][label.strip()] = {
            "wfe": v.get("wfe"),
            "wfe_passed": v.get("wfe_passed"),
            "oos_summary": v["oos_summary"],
            "oos_stress_2x": v["oos_stress_2x"],
            "monte_carlo": v["monte_carlo"],
        }

    # 2) C1_FUNDING_CARRY (180d реальный фандинг, 12 пар)
    print(f"\n[2/3] C1_FUNDING_CARRY — 180-дневный бэктест Spot+Perp ({len(symbols)} пар, плечо 1.5x):", flush=True)
    for label, entry_thr, exit_thr, min_iv in [
        ("Carry Strict (0.025%/8h, 9iv)", 0.00025, 0.00005, 9),
        ("Carry Medium (0.015%/8h, 6iv)", 0.00015, 0.00004, 6),
        ("Carry Broad  (0.010%/8h, 6iv)", 0.00010, 0.00003, 6),
    ]:
        t0 = time.time()
        res_c = bt.run_funding_carry_backtest(
            funding_hist,
            leverage=1.5,
            entry_threshold_8h=entry_thr,
            exit_threshold_8h=exit_thr,
            min_intervals=min_iv,
        )
        print(
            f"{label:32s} | Trades={res_c['total_trades']:3d} | WR={res_c['win_rate_pct']:5.1f}% | "
            f"Net1x={res_c['net_return_1x_pct']:+6.2f}% (APR {res_c['apr_1x_pct']:+5.1f}%) | "
            f"Net2x={res_c['net_return_2x_pct']:+6.2f}% | AvgHold={res_c['avg_hold_days']:4.1f}d | "
            f"MaxDD={res_c['max_drawdown_pct']:4.2f}% | SR={str(res_c['sharpe_ratio']):5s} | {time.time() - t0:.2f}s",
            flush=True,
        )
        results["c1_funding_carry"][label.strip()] = res_c

    # 3) B1_SQUEEZE_BREAKOUT на расширенном пуле 12 пар + фильтр выходных
    print(f"\n[3/3] B1_SQUEEZE_BREAKOUT — расширенный пул ({len(symbols)} пар) + фильтр выходных:", flush=True)
    h1_map = {s: aggregate_1h(b) for s, b in candles.items()}
    h1_ts = {s: [int(x.timestamp) for x in b] for s, b in h1_map.items()}
    fr_ts = {s: [p[0] for p in funding_hist[s]] for s in symbols}
    fr_val = {s: [p[1] for p in funding_hist[s]] for s in symbols}

    def _pit_fr(sym: str, ts_ms: int) -> float:
        arr = fr_ts.get(sym)
        if not arr:
            return 0.0
        pos = bisect_right(arr, ts_ms) - 1
        return fr_val[sym][pos] if pos >= 0 else 0.0

    def _h1_slice(sym: str, hist: List[Any]):
        pos = bisect_right(h1_ts[sym], int(hist[-1].timestamp) - 2_700_000)
        return h1_map[sym][max(0, pos - 180) : pos] if pos >= 55 else None

    fn_b1_base = lambda s, h, o: generate_booster_breakout_signal(
        s, h, reference_open_px=o, bars_1h=_h1_slice(s, h), funding_rate=_pit_fr(s, int(h[-1].timestamp))
    )
    fn_b1_nowkd = lambda s, h, o: (
        None
        if _is_weekend_chop_utc(int(h[-1].timestamp))
        else generate_booster_breakout_signal(
            s, h, reference_open_px=o, bars_1h=_h1_slice(s, h), funding_rate=_pit_fr(s, int(h[-1].timestamp))
        )
    )

    for label, fn, p_kw in [
        (
            "B1 12p Booster (4% risk, 5x)",
            fn_b1_base,
            dict(risk_per_trade_frac=0.04, max_total_risk_frac=0.07, max_leverage=5.0, max_positions=2, allow_compounding=True),
        ),
        (
            "B1 12p + NoWeekendChop (4%, 5x)",
            fn_b1_nowkd,
            dict(risk_per_trade_frac=0.04, max_total_risk_frac=0.07, max_leverage=5.0, max_positions=2, allow_compounding=True),
        ),
        (
            "B1 12p Autotrade (1.5%, 3x, 3p)",
            fn_b1_base,
            dict(risk_per_trade_frac=0.015, max_total_risk_frac=0.045, max_leverage=3.0, max_positions=3, allow_compounding=False),
        ),
        (
            "B1 12p NoWkd Autotrade (1.5%)",
            fn_b1_nowkd,
            dict(risk_per_trade_frac=0.015, max_total_risk_frac=0.045, max_leverage=3.0, max_positions=3, allow_compounding=False),
        ),
    ]:
        t0 = time.time()
        v = bt.run_rolling_wfa(
            candles, strategy_fn=fn, n_cycles=args.cycles, n_trials=4, mc_iterations=args.mc, **p_kw
        )
        print(_fmt_wfa_row(label, v, time.time() - t0), flush=True)
        results["b1_expanded_12p"][label.strip()] = {
            "wfe": v.get("wfe"),
            "wfe_passed": v.get("wfe_passed"),
            "oos_summary": v["oos_summary"],
            "oos_stress_2x": v["oos_stress_2x"],
            "monte_carlo": v["monte_carlo"],
        }

    out_path = BASE_DIR / "data" / "research_intraday12.json"
    atomic_json(out_path, results)
    print(f"\n[SAVED] {out_path}", flush=True)


if __name__ == "__main__":
    main()

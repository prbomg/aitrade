#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрипт воспроизводимой WFA-валидации (180 дней, 6 пар) и MFE/MAE-диагностики выходов AItrade.
Запуск на VPS:
    cd /opt/AItrade && sudo -u aitrade python3 -u scripts/run_wfa_research.py --save-validation
    cd /opt/AItrade && sudo -u aitrade python3 -u scripts/run_wfa_research.py --momentum-only --save-validation
"""

from __future__ import annotations

import argparse
import json
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
from aitrade.config import BASE_DIR
from aitrade.storage import atomic_json
from aitrade.strategies.booster_breakout import generate_booster_breakout_signal
from aitrade.strategies.regime_s1_s3 import aggregate_1h, generate_s1_s3_signal


DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "LINKUSDT"]


def collect_filled_entries(candles: Dict[str, List[Any]], fn) -> List[Any]:
    out = []
    for sym, bars in candles.items():
        i, n = 220, len(bars)
        while i < n - 80:
            sig = fn(sym, bars[max(0, i - 520) : i], bars[i].open)
            if sig:
                fill = None
                for k in range(i, min(i + 3, n - 80)):
                    if bars[k].low <= sig.entry_price <= bars[k].high:
                        fill = k
                        break
                if fill is not None:
                    out.append((sym, bars, fill, sig))
                    i = fill + 24
                    continue
            i += 1
    return out


def eval_exit_policy(bt: WalkForwardBacktester, entries: List[Any], hold_bars: int, use_be: bool, be_r: float = 1.2, tr_r: float = 1.8):
    rs, mfes, maes = [], [], []
    hit_12 = hit_20 = hit_30 = 0
    for sym, bars, fill, sig in entries:
        fut = bars[fill : fill + hold_bars]
        e, sl, tp = sig.entry_price, sig.stop_loss, sig.take_profit_2
        rd = abs(e - sl)
        if rd <= 0 or not fut:
            continue
        if sig.side == "Buy":
            mfe = max((b.high - e) / rd for b in fut)
            mae = max((e - b.low) / rd for b in fut)
        else:
            mfe = max((e - b.low) / rd for b in fut)
            mae = max((b.high - e) / rd for b in fut)
        mfes.append(mfe)
        maes.append(mae)
        if mfe >= 1.2:
            hit_12 += 1
        if mfe >= 2.0:
            hit_20 += 1
        if mfe >= 3.0:
            hit_30 += 1
        atr_f = (float(sig.features.get("atr15", 0)) / e) if e > 0 else 0.015
        _, r_mult, _ = bt._resolve_trade(
            fut, sig.side, e, sl, tp,
            bt.maker_fee, bt.taker_fee + bt.base_slippage, bt.taker_fee + bt.base_slippage,
            use_be, be_trigger_r=be_r, trail_trigger_r=tr_r, trail_frac=max(0.015, 1.8 * atr_f),
        )
        rs.append(r_mult)
    n = max(1, len(rs))
    wr = sum(1 for r in rs if r > 0) / n * 100.0
    wins = sum(r for r in rs if r > 0)
    losses = abs(sum(r for r in rs if r <= 0))
    pf = wins / losses if losses > 0 else 0.0
    mfes.sort()
    maes.sort()
    return n, wr, pf, sum(rs) / n, mfes[n // 2], maes[n // 2], hit_12 / n * 100.0, hit_20 / n * 100.0, hit_30 / n * 100.0


def _fmt_wfa_row(name: str, v: Dict[str, Any], elapsed_s: float) -> str:
    o1, o2, mc = v["oos_summary"], v["oos_stress_2x"], v["monte_carlo"]
    return (
        f"{name:28s} | N={o1['total_trades']:3d} | WR={o1['win_rate_pct']:5.1f}% | PF={str(o1['profit_factor']):4s} | "
        f"Net={o1['net_return_pct']:+6.1f}% | 1xR={o1['avg_r_multiple']:+.3f} | 2xR={o2['avg_r_multiple']:+.3f} | "
        f"WFE={str(v.get('wfe')):5s}({str(v.get('wfe_passed')):5s}) | SR={str(o1['sharpe_ratio']):5s} | "
        f"DSR={str(o1['deflated_sharpe_ratio']):6s} | DD={o1['max_drawdown_pct']:5.1f}% | MC95={mc['p95_max_dd_pct']:5.1f}% | {elapsed_s:.1f}s"
    )


def _merge_and_save_validation(days: int, strategies_updates: Dict[str, Any]) -> Path:
    out_path = BASE_DIR / "data" / "wfa_validation.json"
    existing: Dict[str, Any] = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    merged_strategies = dict(existing.get("strategies") or {})
    merged_strategies.update(strategies_updates)
    payload: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "symbols": DEFAULT_SYMBOLS,
        "strategies": merged_strategies,
    }
    atomic_json(out_path, payload)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--cycles", type=int, default=6)
    ap.add_argument("--mc", type=int, default=2000)
    ap.add_argument("--save-validation", action="store_true")
    ap.add_argument("--skip-wfa", action="store_true")
    ap.add_argument("--momentum-only", action="store_true")
    ap.add_argument("--t1-only", action="store_true")
    args = ap.parse_args()

    from aitrade.config import load_settings
    settings = load_settings()
    bt = WalkForwardBacktester(settings)
    target_syms = list(settings["universe"]["autotrade_pairs"][:12]) if args.t1_only else DEFAULT_SYMBOLS
    candles: Dict[str, List[Any]] = {}
    for s in target_syms:
        t0 = time.time()
        candles[s] = bt.load_historical_klines(s, interval="15", days=args.days)
        print(f"[DATA] {s:8s}: {len(candles[s])} bars ({time.time() - t0:.1f}s)", flush=True)

    funding_hist: Dict[str, List[Tuple[int, float]]] = {}
    for s in target_syms:
        t0 = time.time()
        funding_hist[s] = bt.load_historical_funding(s, days=args.days)
        print(f"[FUND] {s:8s}: {len(funding_hist[s])} rates ({time.time() - t0:.1f}s)", flush=True)

    val_updates: Dict[str, Any] = {}

    if args.t1_only:
        print(f"\n[WFA] {args.cycles}-цикловой Rolling WFA для T1_DONCHIAN_TSMOM ({args.days}д, {len(target_syms)} пар):", flush=True)
        t0 = time.time()
        v_t1 = bt.run_donchian_tsmom_wfa(
            candles,
            funding_history_by_symbol=funding_hist,
            n_cycles=args.cycles,
            n_trials=4,
            mc_iterations=args.mc,
            horizons=(20, 40, 80),
            max_hold_bars=30,
            use_vol_targeting=True,
            risk_per_trade_frac=0.015,
            max_total_risk_frac=0.06,
            max_leverage=3.0,
            max_positions=4,
            allow_compounding=False,
        )
        print(_fmt_wfa_row("T1_DONCHIAN_TSMOM (Vol 12p)", v_t1, time.time() - t0), flush=True)
        o1, o2 = v_t1["oos_summary"], v_t1["oos_stress_2x"]
        val_updates["T1_DONCHIAN_TSMOM"] = {
            "wfe": v_t1.get("wfe"),
            "wfe_passed": bool(v_t1.get("wfe_passed", False)),
            "oos_trades": o1["total_trades"],
            "oos_avg_r": o1["avg_r_multiple"],
            "oos_2x_avg_r": o2["avg_r_multiple"],
            "oos_pf": o1["profit_factor"],
        }
        if args.save_validation:
            out_path = _merge_and_save_validation(args.days, val_updates)
            print(f"\n[SAVED] {out_path}", flush=True)
        return

    if args.momentum_only:
        print(f"\n[WFA] {args.cycles}-цикловой Rolling WFA для AUTOTRADE_MOMENTUM_FUNDING ({args.days}д, {len(DEFAULT_SYMBOLS)} пар):", flush=True)
        t0 = time.time()
        v_base = bt.run_momentum_funding_wfa(
            candles,
            funding_history_by_symbol=funding_hist,
            n_cycles=args.cycles,
            n_trials=4,
            mc_iterations=args.mc,
            liquid_window_only=True,
            hold_bars_override=None,
        )
        print(_fmt_wfa_row("AUTOTRADE_MOMENTUM (8h, Liq)", v_base, time.time() - t0), flush=True)
        o1, o2 = v_base["oos_summary"], v_base["oos_stress_2x"]
        val_updates["AUTOTRADE_MOMENTUM_FUNDING"] = {
            "wfe": v_base.get("wfe"),
            "wfe_passed": bool(v_base.get("wfe_passed", False)),
            "oos_trades": o1["total_trades"],
            "oos_avg_r": o1["avg_r_multiple"],
            "oos_2x_avg_r": o2["avg_r_multiple"],
            "oos_pf": o1["profit_factor"],
        }

        print("\n[DIAG] Чувствительность AUTOTRADE_MOMENTUM_FUNDING (hold / ликвидное окно):", flush=True)
        for label, hold_h, liq in [
            ("Momentum (18h, Liq 07-20)", 18, True),
            ("Momentum (24h, Liq 07-20)", 24, True),
            ("Momentum (8h,  24/7)     ", 8, False),
            ("Momentum (18h, 24/7)     ", 18, False),
            ("Momentum (24h, 24/7)     ", 24, False),
        ]:
            t0 = time.time()
            v_alt = bt.run_momentum_funding_wfa(
                candles,
                funding_history_by_symbol=funding_hist,
                n_cycles=args.cycles,
                n_trials=4,
                mc_iterations=args.mc,
                liquid_window_only=liq,
                hold_bars_override=hold_h,
            )
            print(_fmt_wfa_row(label, v_alt, time.time() - t0), flush=True)

        if args.save_validation:
            out_path = _merge_and_save_validation(args.days, val_updates)
            print(f"\n[SAVED] {out_path}", flush=True)
        return

    h1_map = {s: aggregate_1h(b) for s, b in candles.items()}
    h1_ts = {s: [int(x.timestamp) for x in b] for s, b in h1_map.items()}

    def h1_slice(sym: str, hist: List[Any]):
        pos = bisect_right(h1_ts[sym], int(hist[-1].timestamp) - 2_700_000)
        return h1_map[sym][max(0, pos - 180) : pos] if pos >= 55 else None

    fn_b1 = lambda s, h, o: generate_booster_breakout_signal(s, h, reference_open_px=o, bars_1h=h1_slice(s, h))
    fn_s1 = lambda s, h, o: (lambda g: g if g and g.strategy_id == "S1_TREND_RETEST" else None)(
        generate_s1_s3_signal(s, h, reference_open_px=o, bars_1h=h1_slice(s, h), min_rr_s3=1.15)
    )
    fn_s3 = lambda s, h, o: (lambda g: g if g and g.strategy_id == "S3_VWAP_RANGE" else None)(
        generate_s1_s3_signal(s, h, reference_open_px=o, bars_1h=h1_slice(s, h), min_rr_s3=1.15)
    )

    if not args.skip_wfa:
        print(f"\n[WFA] {args.cycles}-цикловой Rolling WFA ({args.days}д, {len(DEFAULT_SYMBOLS)} пар):", flush=True)
        configs = [
            ("B1_SQUEEZE_BREAKOUT", dict(risk_per_trade_frac=0.04, max_total_risk_frac=0.07, max_leverage=5.0, max_positions=2, allow_compounding=True), fn_b1),
            ("S1_TREND_RETEST", dict(risk_per_trade_frac=0.04, max_total_risk_frac=0.07, max_leverage=5.0, max_positions=2, allow_compounding=True), fn_s1),
            ("S3_VWAP_RANGE", dict(risk_per_trade_frac=0.01, max_total_risk_frac=0.025, max_leverage=3.0, max_positions=3, allow_compounding=False), fn_s3),
        ]
        for name, kw, fn in configs:
            t0 = time.time()
            v = bt.run_rolling_wfa(candles, strategy_fn=fn, n_cycles=args.cycles, n_trials=4, mc_iterations=args.mc, **kw)
            print(_fmt_wfa_row(name, v, time.time() - t0), flush=True)
            o1, o2 = v["oos_summary"], v["oos_stress_2x"]
            val_updates[name] = {
                "wfe": v.get("wfe"),
                "wfe_passed": bool(v.get("wfe_passed", False)),
                "oos_trades": o1["total_trades"],
                "oos_avg_r": o1["avg_r_multiple"],
                "oos_2x_avg_r": o2["avg_r_multiple"],
                "oos_pf": o1["profit_factor"],
            }

        t0 = time.time()
        v_mom = bt.run_momentum_funding_wfa(
            candles,
            funding_history_by_symbol=funding_hist,
            n_cycles=args.cycles,
            n_trials=4,
            mc_iterations=args.mc,
            liquid_window_only=True,
        )
        print(_fmt_wfa_row("AUTOTRADE_MOMENTUM_FUNDING", v_mom, time.time() - t0), flush=True)
        o1, o2 = v_mom["oos_summary"], v_mom["oos_stress_2x"]
        val_updates["AUTOTRADE_MOMENTUM_FUNDING"] = {
            "wfe": v_mom.get("wfe"),
            "wfe_passed": bool(v_mom.get("wfe_passed", False)),
            "oos_trades": o1["total_trades"],
            "oos_avg_r": o1["avg_r_multiple"],
            "oos_2x_avg_r": o2["avg_r_multiple"],
            "oos_pf": o1["profit_factor"],
        }

        if args.save_validation:
            out_path = _merge_and_save_validation(args.days, val_updates)
            print(f"\n[SAVED] {out_path}", flush=True)

    print("\n[MFE/MAE] Диагностика политик выхода (B1 и S1):", flush=True)
    for sname, sfn in [("B1_SQUEEZE_BREAKOUT", fn_b1), ("S1_TREND_RETEST", fn_s1)]:
        entries = collect_filled_entries(candles, sfn)
        print(f"\n=== {sname} (заполненных сигналов: {len(entries)}) ===", flush=True)
        for label, hb, ube, br, tr in [
            ("Current: BE 1.2R + Trail 1.8R + 6h ", 24, True, 1.2, 1.8),
            ("Pure SL/TP (no BE/Trail)      + 6h ", 24, False, 1.2, 1.8),
            ("Pure SL/TP (no BE/Trail)      + 18h", 72, False, 1.2, 1.8),
            ("Wide BE 1.8R + Trail 2.4R     + 18h", 72, True, 1.8, 2.4),
        ]:
            n, wr, pf, er, mfe, mae, p12, p20, p30 = eval_exit_policy(bt, entries, hb, ube, br, tr)
            print(
                f"{label} | WR={wr:5.1f}% | PF={pf:4.2f} | E[R]={er:+.3f} | medMFE={mfe:.2f}R | medMAE={mae:.2f}R | "
                f">=1.2R:{p12:4.1f}% >=2R:{p20:4.1f}% >=3R:{p30:4.1f}%",
                flush=True,
            )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сверка фактического исполнения (SQLite ledgers) с WFA-валидацией (data/wfa_validation.json),
раздельный учёт боевых стратегий T1 / B1 (без 122 legacy-сделок) и сквозная трассировка исполнения.
Запуск на VPS:
    cd /opt/AItrade && sudo -u aitrade python3 -u scripts/live_audit_report.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aitrade.config import BASE_DIR
from aitrade.execution.audit_metrics import compute_live_strategy_metrics
from aitrade.storage import Ledger


def build_live_audit_summary(data_dir: Path) -> Dict[str, Any]:
    wfa_path = data_dir / "wfa_validation.json"
    wfa_strats: Dict[str, Any] = {}
    if wfa_path.exists():
        try:
            wfa_strats = (json.loads(wfa_path.read_text(encoding="utf-8")).get("strategies") or {})
        except Exception:
            wfa_strats = {}

    db_names = (
        ("default", "execution_ledger.sqlite3"),
        ("autotrade", "execution_ledger_autotrade.sqlite3"),
        ("booster", "execution_ledger_booster.sqlite3"),
    )
    by_strategy: Dict[str, List[float]] = {}
    blocked_reasons: Counter = Counter()
    seen_closed = set()
    active_ledgers: List[Tuple[str, Ledger]] = []

    for acc_name, db_file in db_names:
        db_path = data_dir / db_file
        if not db_path.exists():
            continue
        ledger = Ledger(db_path)
        active_ledgers.append((acc_name, ledger))
        try:
            closed = ledger.attribute(ledger.records("closed"))
            for r in closed:
                oid = str(r.get("orderId") or r.get("updatedTime") or "")
                dedup_k = (acc_name, oid, str(r.get("symbol") or ""))
                if dedup_k in seen_closed:
                    continue
                seen_closed.add(dedup_k)
                raw_s = str(r.get("strategy_id") or "UNATTRIBUTED")
                clean_s = raw_s.split(":", 1)[1].strip() if ":" in raw_s else raw_s.strip()
                pnl = float(r.get("closedPnl", 0.0) or 0.0)
                if abs(pnl) < 1e-9:
                    continue
                by_strategy.setdefault(clean_s, []).append(pnl)
        except Exception:
            pass
        try:
            with ledger.connect() as conn:
                rows = conn.execute(
                    "SELECT payload FROM events WHERE kind='ENTRY_BLOCKED' ORDER BY ts DESC LIMIT 200"
                ).fetchall()
                for (raw_p,) in rows:
                    p = json.loads(raw_p)
                    reason = str(p.get("reason") or "unknown")[:70]
                    blocked_reasons[f"{p.get('strategy', '?')}: {reason}"] += 1
        except Exception:
            pass

    strat_report: Dict[str, Any] = {}
    for s_name in sorted(set(by_strategy.keys()) | set(wfa_strats.keys())):
        pnls = by_strategy.get(s_name, [])
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        pf = round(gp / gl, 2) if gl > 0 else (None if gp == 0 else 99.0)
        wr = round((len(wins) / n) * 100.0, 1) if n > 0 else 0.0
        w_info = wfa_strats.get(s_name) or {}
        strat_report[s_name] = {
            "live_trades": n,
            "live_wr_pct": wr,
            "live_pf": pf,
            "live_net_usd": round(gp - gl, 2),
            "wfa_passed": w_info.get("wfe_passed"),
            "wfa_pf": w_info.get("oos_pf"),
            "wfa_2x_r": w_info.get("oos_2x_avg_r"),
        }

    live_metrics = compute_live_strategy_metrics(active_ledgers)

    return {
        "strategies": strat_report,
        "live_strategy_metrics": live_metrics,
        "top_blocked_reasons": blocked_reasons.most_common(10),
    }


def main() -> None:
    rep = build_live_audit_summary(BASE_DIR / "data")
    print("=== AItrade Live Ledger vs WFA Audit ===", flush=True)
    for s_name, m in rep["strategies"].items():
        st = "PASSED" if m["wfa_passed"] is True else ("BLOCKED" if m["wfa_passed"] is False else "N/A")
        print(
            f"{s_name:28s} | WFA={st:7s} (PF={str(m['wfa_pf']):4s}, 2xR={str(m['wfa_2x_r']):6s}) | "
            f"LIVE N={m['live_trades']:3d} | WR={m['live_wr_pct']:5.1f}% | PF={str(m['live_pf']):4s} | Net={m['live_net_usd']:+8.2f}$",
            flush=True,
        )

    lm = rep.get("live_strategy_metrics") or {}
    leg = lm.get("legacy_isolated") or {}
    print(
        f"\n=== Выделенный учёт боевых стратегий (изолировано от {leg.get('trades', 0)} legacy-сделок / {leg.get('net_pnl_usd', 0.0):+.2f}$) ===",
        flush=True,
    )
    for s_id, sm in (lm.get("strategies") or {}).items():
        print(
            f"{s_id:22s} | Closed={sm['trades']:2d} (Open={sm['open_trades']}) | "
            f"Net={sm['net_pnl_usd']:+7.2f}$ | MaxDD={sm['max_drawdown_usd']:6.2f}$ | "
            f"Hold={str(sm['avg_hold_hours']):>5s}h | AvgR={str(sm['avg_realized_r']):>6s} | "
            f"Slip={str(sm['avg_slippage_bps']):>6s}bps | Fees={sm['total_fees_usd']:.3f}$",
            flush=True,
        )

    traces = lm.get("execution_traces") or []
    if traces:
        print("\n=== Сквозная трассировка B1 / T1 (intent -> signal -> fill -> slip -> R) ===", flush=True)
        for tr in traces[:20]:
            print(
                f"{tr['intent_id'][:14]} | {tr['strategy_id']:20s} | {tr['symbol']:9s} {tr['side']:4s} | {tr['status']:8s} | "
                f"Sig={str(tr['signal_entry_px']):>10s} -> Fill={str(tr['actual_fill_px']):>10s} | "
                f"Slip={str(tr['slippage_bps']):>6s}bps | Fee={tr['fees_usd']:.3f}$ | "
                f"Hold={str(tr['hold_hours']):>5s}h | PnL={str(tr['closed_pnl_usd']):>7s}$ | R={str(tr['realized_r']):>6s}",
                flush=True,
            )
    else:
        print("\n=== Сквозная трассировка B1 / T1 ===\n  Пока нет естественных заявок B1/T1 в журнале.", flush=True)

    if rep["top_blocked_reasons"]:
        print("\n=== Top ENTRY_BLOCKED (последние 200 событий) ===", flush=True)
        for reason, cnt in rep["top_blocked_reasons"]:
            print(f"  [{cnt:3d}x] {reason}", flush=True)


if __name__ == "__main__":
    main()

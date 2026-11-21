#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сверка фактического исполнения (SQLite ledgers) с WFA-валидацией (data/wfa_validation.json).
Запуск на VPS:
    cd /opt/AItrade && sudo -u aitrade python3 -u scripts/live_audit_report.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aitrade.config import BASE_DIR
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

    for acc_name, db_file in db_names:
        db_path = data_dir / db_file
        if not db_path.exists():
            continue
        ledger = Ledger(db_path)
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

    return {
        "strategies": strat_report,
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
    if rep["top_blocked_reasons"]:
        print("\n=== Top ENTRY_BLOCKED (последние 200 событий) ===", flush=True)
        for reason, cnt in rep["top_blocked_reasons"]:
            print(f"  [{cnt:3d}x] {reason}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Лаунчер Веб-Центра Управления AItrade для macOS (без устаревшего системного Tcl/Tk 8.5).
Выполняет живое сканирование 20 пар Bybit V5, обновляет многовкладочный дашборд dashboard_report.html
и открывает его в браузере по умолчанию.
"""

from __future__ import annotations

import sys
import webbrowser
from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.dashboard.report_generator import generate_html_dashboard
from aitrade.engine import AITradeCombineEngine


def main() -> None:
    auto_exec = "--demo-trade" in sys.argv
    engine = AITradeCombineEngine()
    scan_res = engine.scan_market(auto_execute_demo=auto_exec)
    bt_res = WalkForwardBacktester(engine.settings).run_comparison_backtest()
    journal = engine.demo_executor.list_recent_trades(limit=40)
    report_path = generate_html_dashboard(scan_res, bt_res, journal)
    webbrowser.open(f"file://{report_path}")


if __name__ == "__main__":
    main()

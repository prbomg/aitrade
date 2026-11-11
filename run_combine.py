"""
Интерфейс командной строки (CLI) для запуска ИИ-комбайна AItrade на Bybit V5.
Команды:
  python3 run_combine.py scan         # Сканировать пул монет Bybit и вывести снайперские сигналы
  python3 run_combine.py demo-trade   # Просканировать рынок, исполнить лучшие сигналы в демо и обновить HTML-дашборд
  python3 run_combine.py backtest     # Запустить сравнительный Walk-Forward бэктест с учетом издержек Bybit
  python3 run_combine.py journal      # Показать журнал открытых демо-сделок (SQLite / Bybit Demo)
  python3 run_combine.py report       # Сгенерировать полный интерактивный HTML-дашборд dashboard_report.html
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.dashboard.report_generator import generate_html_dashboard
from aitrade.engine import AITradeCombineEngine


def run_connection_check() -> None:
    from aitrade.config import load_settings
    from aitrade.exchange.bybit_v5 import BybitV5Client
    import time
    settings = load_settings()
    client = BybitV5Client(settings, allow_offline_fallback=False)
    print("=" * 92)
    print(" ДИАГНОСТИКА ПОДКЛЮЧЕНИЯ BYBIT V5 DEMO TRADING (api-demo.bybit.com / api.bytick.com)")
    print("=" * 92)
    print(f" • Режим работы (AITRADE_MODE):  {client.mode.upper()}")
    masked_key = (
        f"{client.api_key[:4]}...{client.api_key[-3:]} (длина {len(client.api_key)})"
        if len(client.api_key) >= 8
        else "НЕ ЗАДАН"
    )
    print(f" • Ключ BYBIT_DEMO_API_KEY:      {masked_key}")
    print(
        f" • Секрет BYBIT_DEMO_API_SECRET: {'ЗАДАН (длина ' + str(len(client.api_secret)) + ')' if client.api_secret else 'НЕ ЗАДАН'}"
    )

    t0 = time.time()
    try:
        tickers = client.get_tickers("BTCUSDT")
        latency_ms = (time.time() - t0) * 1000.0
        btc_px = tickers[0].get("lastPrice") if tickers else "N/A"
        print(
            f" • Публичный поток котировок:    OK ({client.last_data_source}, BTCUSDT = ${btc_px}, пинг {latency_ms:.1f} мс)"
        )
    except Exception as exc:
        print(f" • Публичный поток котировок:    ОШИБКА СЕТИ ({exc})")

    if client.api_key and client.api_secret:
        try:
            bal = client.get_demo_wallet_balance("USDT")
            acct_list = bal.get("list", [])
            equity = acct_list[0].get("totalEquity", "0") if acct_list else "0"
            avail = acct_list[0].get("totalAvailableBalance", "0") if acct_list else "0"
            print(
                f" • Демо-кошелек Bybit Unified:   OK (Total Equity = {equity} USDT, Доступно = {avail} USDT)"
            )
            positions = client.get_open_positions()
            active_pos = [pos for pos in positions if float(pos.get("size", 0) or 0) > 0]
            print(f" • Открытых демо-позиций:        {len(active_pos)}")
        except Exception as exc:
            print(f" • Демо-кошелек Bybit Unified:   ОШИБКА АВТОРИЗАЦИИ / СЕТИ ({exc})")
    print("=" * 92)


def print_scan_report(report: Dict[str, Any]) -> None:
    print("=" * 92)
    print(
        f" AItrade — СНАЙПЕРСКИЙ ИИ-КОМБАЙН ДЛЯ BYBIT V5 | Источник: {report['data_source']}"
    )
    if report.get("network_note") and report["data_source"] == "deterministic_market_replay":
        print(f" [!] Причина оффлайн-режима: {report['network_note'][:140]}")
    print(
        f" Демо-депозит: ${report['account_equity_usdt']:,.2f} USDT | "
        f"Монет в игре: {report['coins_in_play_count']}/{report['total_pairs_scanned']} | "
        f"Одобрено ИИ-фильтром: {report['approved_sniper_signals_count']} из {report['raw_signals_generated']}"
    )
    print("=" * 92)

    print("\n[1] ТОП-8 АКТИВНЫХ МОНЕТ В ИГРЕ (COINS IN PLAY SCREENER):")
    print(f"{'Пара':<10} {'Score':<7} {'RVOL':<7} {'NATR%':<8} {'Спред%':<9} {'1H Режим (Bias)':<18} {'ADX(1H)':<8}")
    print("-" * 75)
    for r in report["screener_top"][:8]:
        print(
            f"{r['symbol']:<10} {r['in_play_score']:<7.1f} {r['rvol']:<7.2f} "
            f"{r['natr_pct']:<8.3f} {r['spread_pct']:<9.4f} {r['mtf_bias']:<18} {r['adx_1h']:<8.1f}"
        )

    print("\n[2] ОДОБРЕННЫЕ СНАЙПЕРСКИЕ СИГНАЛЫ С МАКСИМАЛЬНЫМ ВИНРЕЙТОМ:")
    if not report["approved_signals"]:
        print("  Нет сигналов, прошедших фильтр Cost-Aware Execution (λ=2.0) и индекс несходства DI<=1.0.")
    for idx, s in enumerate(report["approved_signals"], 1):
        side_label = "LONG (Buy Limit Maker)" if s["side"] == "Buy" else "SHORT (Sell Limit Maker)"
        print("-" * 92)
        print(
            f" Сигнал #{idx}: {s['symbol']} | {side_label} | {s['strategy_name']} [{s['timeframe']}]"
        )
        print(
            f"   • Вероятность успеха (ML Winrate): {s['ml_win_probability'] * 100:.1f}% "
            f"(базовый: {s['base_winrate_est'] * 100:.0f}%) | Ожид. ход E[R]: +{s['expected_return_pct']:.2f}% "
            f"(порог 2x издержек: {s['cost_threshold_pct']:.3f}%) | DI: {s['dissimilarity_index']:.2f}"
        )
        print(
            f"   • Вход: {s['entry_price']} | Stop-Loss: {s['stop_loss']} | "
            f"TP1: {s['take_profit_1']} | TP2: {s['take_profit_2']}"
        )
        print(
            f"   • Размер позиции (0.5% риск): ${s['position_size_usd']:,.2f} ({s['position_qty']} монет, "
            f"плечо {s['effective_leverage']}x) | Ликвидация: {s['liquidation_price']} "
            f"({s['stop_to_liq_multiple']}x дальше стопа) | L2 Slippage: {s['estimated_l2_slippage_bps']} bps"
        )
        for line in s["rationale"]:
            print(f"     - {line}")
        print(f"   • Совет ИИ-агентов: {s['debate_summary']}")

    if report.get("executed_demo_orders"):
        print("\n[3] ИСПОЛНЕННЫЕ ДЕМО-ОРДЕРА (BYBIT DEMO / LOCAL TRACKER):")
        for o in report["executed_demo_orders"]:
            print(
                f"   -> Ордер #{o['trade_id']} [{o['execution_venue']}]: {o['symbol']} {o['side']} "
                f"qty={o['qty']} @ {o['entry_price']} (SL={o['stop_loss']}, TP1={o['take_profit_1']})"
            )
    print("=" * 92)


def run_cli() -> None:
    parser = argparse.ArgumentParser(
        description="AItrade — ИИ-комбайн для внутридневных сигналов и демо-торговли на Bybit V5"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="Проверить подключение к Bybit Demo API")

    scan_p = sub.add_parser("scan", help="Просканировать пул пар Bybit и выдать снайперские сигналы")
    scan_p.add_argument("--json", action="store_true", help="Вывести результат в формате JSON")

    demo_p = sub.add_parser("demo-trade", help="Просканировать и выставить топ-сигналы на демо-счете Bybit")
    demo_p.add_argument("--json", action="store_true", help="Вывести результат в формате JSON")

    bt_p = sub.add_parser("backtest", help="Запустить сравнительный Walk-Forward бэктест")
    bt_p.add_argument("--json", action="store_true", help="Вывести результат в формате JSON")

    sub.add_parser("journal", help="Показать журнал демо-сделок")
    sub.add_parser("report", help="Сгенерировать HTML-дашборд dashboard_report.html")

    args = parser.parse_args()
    cmd = args.command or "scan"

    if cmd == "check":
        run_connection_check()
    elif cmd in ("scan", "demo-trade"):
        engine = AITradeCombineEngine()
        res = engine.scan_market(auto_execute_demo=(cmd == "demo-trade"))
        bt = WalkForwardBacktester(engine.settings).run_comparison_backtest()
        journal = engine.demo_executor.list_recent_trades()
        generate_html_dashboard(res, bt, journal)
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print_scan_report(res)
    elif cmd == "backtest":
        bt = WalkForwardBacktester().run_comparison_backtest()
        if getattr(args, "json", False):
            print(json.dumps(bt, ensure_ascii=False, indent=2))
        else:
            n = bt["naive_baseline"]
            s = bt["aitrade_sniper_combine"]
            print("=" * 88)
            print(" WALK-FORWARD БЭКТЕСТ НА КОТИРОВКАХ BYBIT (УЧЕТ КОМИССИЙ И L2-ПРОСКАЛЬЗЫВАНИЯ)")
            print("=" * 88)
            print(f"{'Показатель':<38} | {'Наивный режим (Taker)':<22} | {'AItrade ИИ-Комбайн':<20}")
            print("-" * 88)
            print(f"{'Количество сделок (Turnover)':<38} | {n['total_trades']:<22} | {s['total_trades']:<20}")
            print(f"{'Win Rate (% прибыльных сделок)':<38} | {n['win_rate_pct']:<21.2f}% | {s['win_rate_pct']:<19.2f}%")
            print(f"{'Profit Factor':<38} | {n['profit_factor']:<22.2f} | {s['profit_factor']:<20.2f}")
            print(f"{'Чистая доходность после комиссий (%)':<38} | {n['net_return_pct']:<21.2f}% | {s['net_return_pct']:<19.2f}%")
            print(f"{'Среднее ожидание на сделку (R)':<38} | {n['avg_r_multiple']:<21.3f}R | {s['avg_r_multiple']:<19.3f}R")
            print(f"{'Максимальная просадка (Max DD %)':<38} | {n['max_drawdown_pct']:<21.2f}% | {s['max_drawdown_pct']:<20.2f}%")
            print(f"{'Sharpe Ratio / Deflated Sharpe (DSR)':<38} | {str(n['sharpe_ratio']) + ' / ' + str(n['deflated_sharpe_ratio']):<22} | {str(s['sharpe_ratio']) + ' / ' + str(s['deflated_sharpe_ratio']):<20}")
            print("=" * 88)
    elif cmd == "journal":
        engine = AITradeCombineEngine()
        rows = engine.demo_executor.list_recent_trades()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif cmd == "report":
        engine = AITradeCombineEngine()
        res = engine.scan_market(auto_execute_demo=False)
        bt = WalkForwardBacktester(engine.settings).run_comparison_backtest()
        journal = engine.demo_executor.list_recent_trades()
        out_file = generate_html_dashboard(res, bt, journal)
        print(f"HTML-дашборд сохранен в: {out_file}")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    run_cli()

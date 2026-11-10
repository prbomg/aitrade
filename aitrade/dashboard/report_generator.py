"""
Генератор интерактивного HTML-отчета и дашборда аналитики AItrade для Bybit V5.
Формирует самодостаточный визуальный терминал dashboard_report.html с:
- Карточками одобренных снайперских сигналов (Точка входа, SL, TP1/TP2, R:R, ML Winrate %, DI, сводка дебатов Бычьего и Медвежьего ИИ-агентов),
- Таблицей пре-скринера «Монеты в игре» (Coins in Play: RVOL, NATR, Spread, 1H Bias, ADX),
- Сравнительной таблицей Walk-Forward бэктеста (Наивный режим против ИИ-комбайна),
- Журналом демо-сделок на Bybit Unified Demo / SQLite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from aitrade.config import BASE_DIR


def generate_html_dashboard(
    scan_data: Dict[str, Any],
    backtest_data: Dict[str, Any],
    journal_rows: List[Dict[str, Any]],
    output_path: Path = BASE_DIR / "dashboard_report.html",
) -> Path:
    signals_html = ""
    for s in scan_data.get("approved_signals", []):
        badge_cls = "buy" if s["side"] == "Buy" else "sell"
        side_txt = "LONG (Limit Maker)" if s["side"] == "Buy" else "SHORT (Limit Maker)"
        rat_items = "".join(f"<li>{r}</li>" for r in s.get("rationale", []))
        signals_html += f"""
        <div class="signal-card">
          <div class="sig-header">
            <div>
              <strong style="font-size:16px;">{s['symbol']}</strong>
              <span class="badge {badge_cls}">{side_txt}</span>
              <span class="muted">{s['strategy_name']} ({s['timeframe']})</span>
            </div>
            <span class="badge win">ML Winrate: {s['ml_win_probability'] * 100:.1f}% | E[R]: +{s['expected_return_pct']}%</span>
          </div>
          <div class="sig-grid">
            <div>Вход: <strong>{s['entry_price']}</strong></div>
            <div>Stop-Loss: <strong class="red">{s['stop_loss']}</strong></div>
            <div>Take-Profit 1: <strong class="green">{s['take_profit_1']}</strong></div>
            <div>Take-Profit 2: <strong class="green">{s['take_profit_2']}</strong></div>
            <div>Позиция: <strong>${s['position_size_usd']} ({s['effective_leverage']}x)</strong></div>
            <div>Дист. до ликв.: <strong>{s['stop_to_liq_multiple']}x стопа</strong></div>
            <div>L2 Slippage: <strong>{s['estimated_l2_slippage_bps']} bps</strong></div>
            <div>Индекс DI: <strong>{s['dissimilarity_index']} &le; 1.0</strong></div>
          </div>
          <ul class="rationale-list">{rat_items}</ul>
          <div class="debate-box">{s['debate_summary']}</div>
        </div>
        """

    if not signals_html:
        signals_html = "<p class='muted'>Нет активных сигналов, прошедших фильтр Cost-Aware (&lambda;=2.0).</p>"

    scr_rows = ""
    for r in scan_data.get("screener_top", [])[:15]:
        status_tag = (
            "<span class='green'>В игре</span>"
            if r["passed"]
            else "<span class='red'>Отсев</span>"
        )
        scr_rows += f"""
        <tr>
          <td><strong>{r['symbol']}</strong></td>
          <td>{r['in_play_score']}</td>
          <td>{r['rvol']}x</td>
          <td>{r['natr_pct']}%</td>
          <td>{r['spread_pct']}%</td>
          <td>{r['mtf_bias']} (ADX {r['adx_1h']})</td>
          <td>{status_tag}</td>
        </tr>
        """

    j_rows = ""
    for row in journal_rows[:15]:
        j_rows += f"""
        <tr>
          <td>#{row['id']}</td>
          <td>{row['created_at']}</td>
          <td><strong>{row['symbol']}</strong></td>
          <td>{row['side']}</td>
          <td>{row['entry_price']}</td>
          <td>{row['stop_loss']} / {row['take_profit_1']}</td>
          <td>${row['notional_usd']} ({row['leverage']}x)</td>
          <td>{row['ml_win_prob'] * 100:.1f}%</td>
          <td>{row['execution_venue']}</td>
        </tr>
        """
    if not j_rows:
        j_rows = "<tr><td colspan='9' class='muted'>Журнал демо-сделок пуст</td></tr>"

    n = backtest_data["naive_baseline"]
    s = backtest_data["aitrade_sniper_combine"]

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <title>AItrade — Аналитический терминал ИИ-комбайна (Bybit V5 Demo & Live)</title>
  <style>
    body {{ margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #e2e8f0; }}
    h1 {{ margin: 0; font-size: 22px; color: #fff; }}
    .muted {{ color: #94a3b8; font-size: 13px; }}
    .green {{ color: #10b981; }}
    .red {{ color: #ef4444; }}
    .metrics-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin: 20px 0; }}
    .metric-box {{ background: #131a2a; border: 1px solid #232f46; padding: 14px 16px; border-radius: 10px; }}
    .metric-val {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1.25fr 1fr; gap: 20px; }}
    @media (max-width: 1100px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
    .panel {{ background: #131a2a; border: 1px solid #232f46; border-radius: 12px; padding: 18px; margin-bottom: 20px; }}
    .panel h2 {{ margin-top: 0; font-size: 17px; border-bottom: 1px solid #232f46; padding-bottom: 10px; }}
    .signal-card {{ border: 1px solid #232f46; background: #0f1523; border-radius: 10px; padding: 14px; margin-bottom: 14px; }}
    .sig-header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
    .sig-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-top: 10px; font-size: 13px; }}
    .badge {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 12px; font-weight: 700; margin: 0 4px; }}
    .badge.buy {{ background: rgba(16, 185, 129, 0.18); color: #10b981; }}
    .badge.sell {{ background: rgba(239, 68, 68, 0.18); color: #ef4444; }}
    .badge.win {{ background: rgba(245, 158, 11, 0.2); color: #f59e0b; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid #232f46; }}
    th {{ color: #94a3b8; }}
    .rationale-list {{ margin: 8px 0; padding-left: 18px; color: #cbd5e1; font-size: 13px; }}
    .debate-box {{ background: rgba(59, 130, 246, 0.09); border-left: 3px solid #3b82f6; padding: 8px 10px; font-size: 12px; color: #cbd5e1; margin-top: 8px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>AItrade — Снайперский ИИ-комбайн для Bybit V5 (Demo & Live)</h1>
  <div class="muted">Воронка: Coins in Play Screener &rarr; 5 Интрадей-Архетипов &rarr; ML Meta-Labeling (&lambda;=2.0, DI&le;1.0) &rarr; Multi-Agent Debate &rarr; RiskEngine (5x Liq Rule)</div>

  <div class="metrics-row">
    <div class="metric-box"><div class="muted">Источник данных</div><div class="metric-val" style="color:#f59e0b;font-size:17px;">{scan_data['data_source']}</div></div>
    <div class="metric-box"><div class="muted">Демо-депозит (USDT)</div><div class="metric-val">${scan_data['account_equity_usdt']:,.2f}</div></div>
    <div class="metric-box"><div class="muted">Монет в игре (Screener)</div><div class="metric-val">{scan_data['coins_in_play_count']} / {scan_data['total_pairs_scanned']}</div></div>
    <div class="metric-box"><div class="muted">Сырых сетапов &rarr; Одобрено ИИ</div><div class="metric-val green">{scan_data['raw_signals_generated']} &rarr; {scan_data['approved_sniper_signals_count']}</div></div>
  </div>

  <div class="grid-2">
    <div>
      <div class="panel">
        <h2>Одобренные снайперские сигналы (Высокий Winrate 65%–75%+)</h2>
        {signals_html}
      </div>
      <div class="panel">
        <h2>Журнал демо-сделок (Bybit Unified Demo / Local SQLite)</h2>
        <table>
          <thead><tr><th>ID</th><th>Время</th><th>Пара</th><th>Сторона</th><th>Вход</th><th>SL / TP1</th><th>Объем ($)</th><th>WinProb</th><th>Статус</th></tr></thead>
          <tbody>{j_rows}</tbody>
        </table>
      </div>
    </div>
    <div>
      <div class="panel">
        <h2>Пре-скринер «Монеты в игре» (Coins in Play — Bybit V5)</h2>
        <table>
          <thead><tr><th>Пара</th><th>Score</th><th>RVOL</th><th>NATR</th><th>Спред</th><th>1H Режим</th><th>Статус</th></tr></thead>
          <tbody>{scr_rows}</tbody>
        </table>
      </div>
      <div class="panel">
        <h2>Walk-Forward Бэктест (Наивный режим vs Снайперский ИИ-комбайн)</h2>
        <table>
          <thead><tr><th>Метрика</th><th>Наивный режим (Taker)</th><th>AItrade Комбайн (Maker + ML)</th></tr></thead>
          <tbody>
            <tr><td>Всего сделок (Turnover)</td><td>{n['total_trades']}</td><td><strong>{s['total_trades']}</strong></td></tr>
            <tr><td>Win Rate (%)</td><td>{n['win_rate_pct']}%</td><td><strong class="green">{s['win_rate_pct']}%</strong></td></tr>
            <tr><td>Profit Factor</td><td>{n['profit_factor']}</td><td><strong class="green">{s['profit_factor']}</strong></td></tr>
            <tr><td>Чистая доходность (%)</td><td>{n['net_return_pct']}%</td><td><strong class="green">+{s['net_return_pct']}%</strong></td></tr>
            <tr><td>Среднее ожидание (R)</td><td>{n['avg_r_multiple']}R</td><td><strong class="green">{s['avg_r_multiple']}R</strong></td></tr>
            <tr><td>Макс. просадка (Max DD)</td><td>{n['max_drawdown_pct']}%</td><td><strong>{s['max_drawdown_pct']}%</strong></td></tr>
            <tr><td>Sharpe / Deflated Sharpe</td><td>{n['sharpe_ratio']} / {n['deflated_sharpe_ratio']}</td><td><strong>{s['sharpe_ratio']} / {s['deflated_sharpe_ratio']}</strong></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path

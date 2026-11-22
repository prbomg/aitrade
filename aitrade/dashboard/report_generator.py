"""
Генератор интерактивного веб-приложения и дашборда аналитики AItrade для Bybit V5 (dashboard_report.html).
Формирует полноценный многовкладочный (Tabbed UI) веб-интерфейс институционального уровня:
- Вкладка 1: 🎯 Снайперские сигналы (Winrate 65%–85%) + фильтр поиска + детализация дебатов ИИ-агентов
- Вкладка 2: 📊 Скринер «Монеты в игре» (Coins in Play — 20 пар Bybit V5 с сортировкой и фильтрацией)
- Вкладка 3: 📒 Демо-Портфель и Журнал ордеров (Bybit Unified Demo / Local SQLite)
- Вкладка 4: 📈 Walk-Forward Бэктест и Аналитика подхода (сравнение с наивным режимом и узкие места Bybit)
- Вкладка 5: ⚙️ Конфигурация системы и API-шлюзов Bybit V5
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List

from aitrade.config import BASE_DIR, load_settings


def generate_html_dashboard(
    scan_data: Dict[str, Any],
    backtest_data: Dict[str, Any],
    journal_rows: List[Dict[str, Any]],
    output_path: Path = BASE_DIR / "data" / "static_report.html",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    signals = scan_data.get("approved_signals", [])
    rejected = scan_data.get("rejected_signals", [])
    screener_rows = scan_data.get("screener_top", [])

    best_prob = (
        f"{signals[0]['ml_win_probability'] * 100:.1f}%"
        if signals
        else "Ожидание"
    )
    best_sym = (
        f"{signals[0]['symbol']} ({signals[0]['strategy_id']})"
        if signals
        else "Нет активного сетапа"
    )

    signals_html = ""
    for idx, s in enumerate(signals, 1):
        is_buy = s["side"] == "Buy"
        pill_cls = "pill-long" if is_buy else "pill-short"
        side_txt = "LONG • Limit Maker" if is_buy else "SHORT • Limit Maker"
        rr_tp1 = abs(s["take_profit_1"] - s["entry_price"]) / max(
            abs(s["entry_price"] - s["stop_loss"]), 1e-9
        )
        rr_tp2 = abs(s["take_profit_2"] - s["entry_price"]) / max(
            abs(s["entry_price"] - s["stop_loss"]), 1e-9
        )
        rat_items = "".join(f"<li>{r}</li>" for r in s.get("rationale", []))
        signals_html += f"""
        <div class="sig-card" data-symbol="{s['symbol']}" data-side="{s['side']}">
          <div class="sig-top">
            <div class="sig-title-row">
              <span class="sig-rank">#{idx}</span>
              <span class="sig-symbol">{s['symbol']}</span>
              <span class="pill {pill_cls}">{side_txt}</span>
              <span class="pill pill-tf">{s['timeframe']}</span>
              <span class="sig-strat">{s['strategy_name']}</span>
            </div>
            <div class="sig-badges">
              <span class="pill pill-win">ML Winrate: {s['ml_win_probability'] * 100:.1f}%</span>
              <span class="pill pill-edge">E[R]: +{s['expected_return_pct']}% (Порог: {s['cost_threshold_pct']}%)</span>
            </div>
          </div>

          <div class="sig-metrics-grid">
            <div class="m-cell"><span class="m-lbl">Точка входа (Limit)</span><span class="m-val">{s['entry_price']}</span></div>
            <div class="m-cell"><span class="m-lbl">Stop-Loss (За хвост)</span><span class="m-val red">{s['stop_loss']} (-{abs(s['entry_price'] - s['stop_loss']) / max(s['entry_price'], 1e-9) * 100:.2f}%)</span></div>
            <div class="m-cell"><span class="m-lbl">Take-Profit 1 ({rr_tp1:.1f}R)</span><span class="m-val green">{s['take_profit_1']}</span></div>
            <div class="m-cell"><span class="m-lbl">Take-Profit 2 ({rr_tp2:.1f}R)</span><span class="m-val green">{s['take_profit_2']}</span></div>
            <div class="m-cell"><span class="m-lbl">Объем позиции (0.5% риск)</span><span class="m-val">${s['position_size_usd']:,.2f} ({s['effective_leverage']}x)</span></div>
            <div class="m-cell"><span class="m-lbl">Запас до ликвидации</span><span class="m-val amber">{s['stop_to_liq_multiple']}x стопа ({s['liquidation_price']})</span></div>
            <div class="m-cell"><span class="m-lbl">Проскальзывание стакана L2</span><span class="m-val">{s['estimated_l2_slippage_bps']} bps</span></div>
            <div class="m-cell"><span class="m-lbl">Индекс несходства (DI)</span><span class="m-val green">{s['dissimilarity_index']} &le; 1.0</span></div>
          </div>

          <div class="sig-body">
            <div class="sig-rationale-title">Факторы входа и микроструктура стакана L2:</div>
            <ul class="rationale-list">{rat_items}</ul>
            <div class="debate-banner">
              <strong>🤖 Совет ИИ-агентов (Bull vs Bear Debate):</strong> {s['debate_summary']}
            </div>
            <div class="invalidation-line">
              <strong>⚠️ Правило отмены / выхода:</strong> {s.get('invalidation_rule', 'Закрытие за уровнем SL или правило 2 баров')}
            </div>
          </div>
        </div>
        """

    if not signals_html:
        signals_html = """
        <div class="empty-state">
          <h3>В данный момент нет снайперских сигналов, прошедших фильтр Cost-Aware (&lambda;=2.0)</h3>
          <p>Рынок находится в фазе низкого математического ожидания или повышенного шума. Нажмите «Сканировать Bybit» позже.</p>
        </div>
        """

    rejected_html = ""
    for r_sig in rejected[:8]:
        rejected_html += f"""
        <tr>
          <td><strong>{r_sig['symbol']}</strong></td>
          <td>{r_sig['strategy_id']}</td>
          <td>{r_sig['side']}</td>
          <td>{r_sig['ml_win_probability'] * 100:.1f}%</td>
          <td>+{r_sig['expected_return_pct']}% / {r_sig['cost_threshold_pct']}%</td>
          <td>{r_sig['dissimilarity_index']}</td>
          <td><span class="pill pill-short">Отсеян ИИ-фильтром</span></td>
        </tr>
        """
    if not rejected_html:
        rejected_html = "<tr><td colspan='7' class='muted'>Отклоненных кандидатов на текущем баре нет</td></tr>"

    scr_rows_html = ""
    for r in screener_rows:
        st_badge = (
            "<span class='pill pill-long'>В игре</span>"
            if r["passed"]
            else f"<span class='pill pill-short' title='{r.get('reject_reason') or ''}'>Отсев</span>"
        )
        playbooks_str = ", ".join(r.get("recommended_playbooks", [])[:2])
        scr_rows_html += f"""
        <tr>
          <td><strong>{r['symbol']}</strong></td>
          <td><strong class="amber">{r['in_play_score']}</strong></td>
          <td>{r['rvol']}x</td>
          <td>{r['natr_pct']}%</td>
          <td>{r['atr_expansion_ratio']}x</td>
          <td>{r['spread_pct']}%</td>
          <td>${r['turnover_24h_usd'] / 1e6:,.1f}M</td>
          <td>{r['funding_rate'] * 100:+.4f}%</td>
          <td><strong>{r['mtf_bias']}</strong> (ADX {r['adx_1h']})</td>
          <td class="muted">{playbooks_str}</td>
          <td>{st_badge}</td>
        </tr>
        """

    j_rows_html = ""
    for o in scan_data.get("live_open_orders", []):
        o_badge = "<span class='pill pill-long'>LONG (BYBIT LIVE ORDER)</span>" if o.get("side") == "Buy" else "<span class='pill pill-short'>SHORT (BYBIT LIVE ORDER)</span>"
        j_rows_html += f"""
        <tr style="background:rgba(16,185,129,0.06);">
          <td><strong>{str(o.get('orderId', ''))[:8]}</strong></td>
          <td>ACTIVE ({o.get('orderStatus', 'New')})</td>
          <td><strong>{o.get('symbol', '')}</strong></td>
          <td>{o_badge}</td>
          <td>{o.get('orderType', 'Limit')} ({o.get('timeInForce', 'GTC')})</td>
          <td>{o.get('price', '')}</td>
          <td class="red">{o.get('stopLoss') or '—'}</td>
          <td class="green">{o.get('takeProfit') or '—'}</td>
          <td><strong>qty={o.get('qty', '')}</strong></td>
          <td><span class="pill pill-win">В СТАКАНЕ BYBIT</span></td>
          <td><code>/v5/order/realtime</code></td>
        </tr>
        """
    for p in scan_data.get("live_open_positions", []):
        p_badge = "<span class='pill pill-long'>LONG POSITION</span>" if p.get("side") == "Buy" else "<span class='pill pill-short'>SHORT POSITION</span>"
        j_rows_html += f"""
        <tr style="background:rgba(59,130,246,0.08);">
          <td><strong>POS</strong></td>
          <td>OPEN POSITION</td>
          <td><strong>{p.get('symbol', '')}</strong></td>
          <td>{p_badge}</td>
          <td>Leverage {p.get('leverage', '1')}x</td>
          <td>{p.get('avgPrice', '')}</td>
          <td class="red">{p.get('stopLoss') or '—'}</td>
          <td class="green">{p.get('takeProfit') or '—'}</td>
          <td><strong>size={p.get('size', '')}</strong></td>
          <td><span class="pill pill-edge">PnL: ${p.get('unrealisedPnl', '0')}</span></td>
          <td><code>/v5/position/list</code></td>
        </tr>
        """
    for row in journal_rows[:30]:
        s_badge = (
            "<span class='pill pill-long'>LONG</span>"
            if row["side"] == "Buy"
            else "<span class='pill pill-short'>SHORT</span>"
        )
        j_rows_html += f"""
        <tr>
          <td><strong>#{row['id']}</strong></td>
          <td>{row['created_at']}</td>
          <td><strong>{row['symbol']}</strong></td>
          <td>{s_badge}</td>
          <td>{row['strategy_id']}</td>
          <td>{row['entry_price']}</td>
          <td class="red">{row['stop_loss']}</td>
          <td class="green">{row['take_profit_1']}</td>
          <td><strong>${row['notional_usd']:,.2f}</strong> ({row['leverage']}x)</td>
          <td><span class="pill pill-win">{row['ml_win_prob'] * 100:.1f}%</span></td>
          <td><code>{row['execution_venue']}</code></td>
        </tr>
        """
    if not j_rows_html:
        j_rows_html = "<tr><td colspan='11' class='muted'>Журнал демо-сделок пока пуст</td></tr>"

    n = backtest_data["naive_baseline"]
    s = backtest_data["aitrade_sniper_combine"]
    turnover_reduction = round(
        (1.0 - s["total_trades"] / max(n["total_trades"], 1)) * 100.0, 1
    )

    risk_cfg = settings.get("risk", {})
    ml_cfg = settings.get("ml_filter", {})
    scr_cfg = settings.get("screener", {})
    ex_cfg = settings.get("exchange", {})
    has_demo_key = bool(ex_cfg.get("demo_api_key"))

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AItrade Control Center — Снайперский ИИ-комбайн Bybit V5</title>
  <style>
    :root {{
      --bg: #070b14;
      --surface: #0f172a;
      --card: #131c31;
      --border: #1e293b;
      --text: #f8fafc;
      --muted: #94a3b8;
      --amber: #f59e0b;
      --green: #10b981;
      --red: #ef4444;
      --blue: #3b82f6;
      --indigo: #6366f1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}
    .topbar {{
      background: linear-gradient(90deg, #0b1120 0%, #111827 100%);
      border-bottom: 1px solid var(--border);
      padding: 16px 28px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      position: sticky;
      top: 0;
      z-index: 100;
    }}
    .brand {{ display: flex; align-items: center; gap: 14px; }}
    .logo-badge {{
      width: 42px; height: 42px; border-radius: 11px;
      background: linear-gradient(135deg, var(--amber), #ea580c);
      color: #000; font-weight: 900; font-size: 19px;
      display: flex; align-items: center; justify-content: center;
      box-shadow: 0 4px 14px rgba(245, 158, 11, 0.35);
    }}
    .brand h1 {{ margin: 0; font-size: 20px; font-weight: 800; letter-spacing: -0.3px; }}
    .brand-sub {{ color: var(--muted); font-size: 12.5px; margin-top: 2px; }}
    .status-pills {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .status-chip {{
      background: #131c31;
      border: 1px solid #263554;
      padding: 7px 13px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .dot-live {{ width: 8px; height: 8px; border-radius: 50%; background: var(--green); display: inline-block; box-shadow: 0 0 8px var(--green); }}
    .container {{ max-width: 1480px; margin: 0 auto; padding: 22px 28px 48px; }}

    /* KPI ROW */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      margin-bottom: 22px;
    }}
    .kpi-box {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px 18px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.22);
    }}
    .kpi-lbl {{ color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px; }}
    .kpi-val {{ font-size: 25px; font-weight: 800; margin-top: 6px; }}
    .kpi-sub {{ color: var(--muted); font-size: 11.5px; margin-top: 4px; }}

    /* NAVIGATION TABS */
    .nav-tabs {{
      display: flex;
      gap: 8px;
      background: #0b1120;
      padding: 8px;
      border-radius: 12px;
      border: 1px solid var(--border);
      margin-bottom: 22px;
      flex-wrap: wrap;
    }}
    .tab-btn {{
      background: transparent;
      color: var(--muted);
      border: none;
      padding: 11px 18px;
      border-radius: 9px;
      font-size: 13.5px;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }}
    .tab-btn:hover {{ color: #fff; background: rgba(255, 255, 255, 0.04); }}
    .tab-btn.active {{
      background: var(--blue);
      color: #fff;
      box-shadow: 0 4px 12px rgba(59, 130, 246, 0.35);
    }}
    .tab-count {{
      background: rgba(0, 0, 0, 0.28);
      padding: 2px 7px;
      border-radius: 99px;
      font-size: 11px;
    }}
    .tab-pane {{ display: none; }}
    .tab-pane.active {{ display: block; animation: fadeIn 0.2s ease; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(3px); }} to {{ opacity: 1; transform: translateY(0); }} }}

    /* PANELS & SIGNAL CARDS */
    .panel {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 20px 22px;
      margin-bottom: 20px;
    }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      margin-bottom: 16px;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .panel-header h2 {{ margin: 0; font-size: 17px; font-weight: 700; }}
    .filter-bar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .search-input {{
      background: #070b14;
      border: 1px solid #263554;
      color: #fff;
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 13px;
    }}

    .sig-card {{
      background: var(--card);
      border: 1px solid #263554;
      border-radius: 13px;
      padding: 18px;
      margin-bottom: 16px;
      transition: border-color 0.15s ease;
    }}
    .sig-card:hover {{ border-color: var(--blue); }}
    .sig-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
    }}
    .sig-title-row {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .sig-rank {{ color: var(--amber); font-weight: 800; font-size: 14px; }}
    .sig-symbol {{ font-size: 19px; font-weight: 800; letter-spacing: -0.3px; }}
    .sig-strat {{ color: #cbd5e1; font-size: 13.5px; font-weight: 600; margin-left: 4px; }}
    .sig-badges {{ display: flex; gap: 8px; flex-wrap: wrap; }}

    .pill {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 7px;
      font-size: 11.5px;
      font-weight: 700;
    }}
    .pill-long {{ background: rgba(16, 185, 129, 0.16); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.3); }}
    .pill-short {{ background: rgba(239, 68, 68, 0.16); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.3); }}
    .pill-tf {{ background: rgba(148, 163, 184, 0.14); color: #cbd5e1; }}
    .pill-win {{ background: rgba(245, 158, 11, 0.18); color: var(--amber); border: 1px solid rgba(245, 158, 11, 0.35); }}
    .pill-edge {{ background: rgba(59, 130, 246, 0.16); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }}

    .sig-metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
      gap: 10px;
      background: #090e1a;
      border: 1px solid var(--border);
      padding: 12px 14px;
      border-radius: 10px;
      margin-bottom: 12px;
    }}
    .m-cell {{ display: flex; flex-direction: column; }}
    .m-lbl {{ font-size: 11px; color: var(--muted); }}
    .m-val {{ font-size: 13.5px; font-weight: 700; margin-top: 2px; }}

    .sig-rationale-title {{ font-size: 12px; font-weight: 700; color: var(--muted); text-transform: uppercase; margin-bottom: 4px; }}
    .rationale-list {{ margin: 4px 0 10px; padding-left: 18px; color: #e2e8f0; font-size: 13px; }}
    .rationale-list li {{ margin-bottom: 3px; }}
    .debate-banner {{
      background: rgba(59, 130, 246, 0.09);
      border-left: 3px solid var(--blue);
      padding: 9px 12px;
      border-radius: 6px;
      font-size: 12.5px;
      color: #dbeafe;
      margin-bottom: 8px;
    }}
    .invalidation-line {{
      font-size: 12px;
      color: #fca5a5;
      background: rgba(239, 68, 68, 0.07);
      padding: 6px 10px;
      border-radius: 6px;
    }}

    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--border); }}
    th {{ color: var(--muted); font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.4px; }}
    tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}

    .green {{ color: var(--green); }}
    .red {{ color: var(--red); }}
    .amber {{ color: var(--amber); }}
    .muted {{ color: var(--muted); }}

    .arch-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 14px;
      margin-top: 14px;
    }}
    .arch-box {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
    }}
    .arch-box h4 {{ margin: 0 0 6px 0; color: var(--amber); font-size: 14px; }}
    .arch-box p {{ margin: 0; font-size: 12.5px; color: #cbd5e1; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <div class="logo-badge">AI</div>
      <div>
        <h1>AItrade Control Center — Снайперский ИИ-комбайн Bybit V5</h1>
        <div class="brand-sub">Воронка: Coins in Play Screener &rarr; 5 Интрадей-Архетипов &rarr; ML Meta-Labeling (&lambda;=2.0, DI&le;1.0) &rarr; Multi-Agent Debate &rarr; RiskEngine (5x Liq Rule)</div>
      </div>
    </div>
    <div class="status-pills">
      <span class="status-chip"><span class="dot-live"></span> Источник: <strong>{scan_data['data_source']}</strong></span>
      <span class="status-chip">🔑 Ключи Bybit Demo: <strong>{'ПОДКЛЮЧЕНЫ' if has_demo_key else 'ЛОКАЛЬНЫЙ РЕЖИМ'}</strong></span>
      <span class="status-chip">🕒 Обновлено: <strong>{now_str}</strong></span>
    </div>
  </div>

  <div class="container">
    <!-- KPI METRICS -->
    <div class="kpi-grid">
      <div class="kpi-box">
        <div class="kpi-lbl">Демо-депозит (Bybit Unified USDT)</div>
        <div class="kpi-val">${scan_data['account_equity_usdt']:,.2f}</div>
        <div class="kpi-sub">Фиксированный риск: {risk_cfg.get('risk_per_trade_pct', 0.005)*100:.2f}% на сделку | Плечо &le; {risk_cfg.get('max_portfolio_leverage', 4.0)}x</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-lbl">Монет «в игре» (Pre-Screener)</div>
        <div class="kpi-val">{scan_data['coins_in_play_count']} / {scan_data['total_pairs_scanned']}</div>
        <div class="kpi-sub">Спред &lt; {scr_cfg.get('max_spread_pct', 0.10)}% | Защита ATR14/ATR20 &le; 1.5x</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-lbl">Воронка ИИ-фильтрации</div>
        <div class="kpi-val green">{scan_data['raw_signals_generated']} &rarr; {scan_data['approved_sniper_signals_count']}</div>
        <div class="kpi-sub">Фильтр издержек Cost-Aware (&lambda;={ml_cfg.get('cost_selectivity_lambda', 2.0)}) и DI &le; {ml_cfg.get('di_threshold', 1.0)}</div>
      </div>
      <div class="kpi-box">
        <div class="kpi-lbl">Топ-1 Сигнал (ML Winrate)</div>
        <div class="kpi-val amber">{best_prob}</div>
        <div class="kpi-sub">{best_sym}</div>
      </div>
    </div>

    <!-- NAVIGATION TABS -->
    <div class="nav-tabs">
      <button class="tab-btn active" onclick="openTab('pane-signals', this)">
        🎯 1. Снайперские сигналы <span class="tab-count">{len(signals)}</span>
      </button>
      <button class="tab-btn" onclick="openTab('pane-screener', this)">
        📊 2. Скринер «Монеты в игре» <span class="tab-count">{len(screener_rows)}</span>
      </button>
      <button class="tab-btn" onclick="openTab('pane-journal', this)">
        📒 3. Демо-Портфель и Ордера <span class="tab-count">{len(journal_rows)}</span>
      </button>
      <button class="tab-btn" onclick="openTab('pane-backtest', this)">
        📈 4. Walk-Forward Бэктест и Аналитика
      </button>
      <button class="tab-btn" onclick="openTab('pane-settings', this)">
        ⚙️ 5. Параметры Риска и Шлюзов Bybit
      </button>
    </div>

    <!-- ВКЛАДКА 1: СНАЙПЕРСКИЕ СИГНАЛЫ -->
    <div id="pane-signals" class="tab-pane active">
      <div class="panel">
        <div class="panel-header">
          <h2>Одобренные снайперские сигналы с максимальным винрейтом (Bybit V5)</h2>
          <div class="filter-bar">
            <input type="text" id="sigSearch" class="search-input" placeholder="Фильтр по монете (напр. XRP, ADA)..." oninput="filterSignals()" />
          </div>
        </div>
        <div id="signalsContainer">{signals_html}</div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <h2>Отсеянные ИИ-фильтром кандидаты (Защита от комиссий и шума)</h2>
        </div>
        <table>
          <thead>
            <tr><th>Пара</th><th>Архетип</th><th>Сторона</th><th>ML Winrate</th><th>Ожид. ход / Порог 2x</th><th>Индекс DI</th><th>Статус</th></tr>
          </thead>
          <tbody>{rejected_html}</tbody>
        </table>
      </div>
    </div>

    <!-- ВКЛАДКА 2: СКРИНЕР МОНЕТ В ИГРЕ -->
    <div id="pane-screener" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <h2>Четырехступенчатый пре-скринер «Монеты в игре» (Coins in Play — 20 фьючерсных пар Bybit)</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th>Пара</th>
              <th>In-Play Score</th>
              <th>RVOL (Отн. объем)</th>
              <th>NATR (%)</th>
              <th>ATR14/ATR20</th>
              <th>Бид-Аск Спред</th>
              <th>Оборот 24ч</th>
              <th>Фандинг</th>
              <th>Старший 1H Режим</th>
              <th>Активные плейбуки</th>
              <th>Допуск</th>
            </tr>
          </thead>
          <tbody>{scr_rows_html}</tbody>
        </table>
      </div>
    </div>

    <!-- ВКЛАДКА 3: ЖУРНАЛ ДЕМО-СДЕЛОК -->
    <div id="pane-journal" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <h2>Журнал демо-сделок (Bybit Unified Demo API / Локальное хранилище SQLite)</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Время UTC</th>
              <th>Пара</th>
              <th>Направление</th>
              <th>Стратегия</th>
              <th>Цена входа</th>
              <th>Stop-Loss</th>
              <th>Take-Profit 1</th>
              <th>Размер позиции ($)</th>
              <th>ML Winrate</th>
              <th>Контур исполнения</th>
            </tr>
          </thead>
          <tbody>{j_rows_html}</tbody>
        </table>
      </div>
    </div>

    <!-- ВКЛАДКА 4: WALK-FORWARD БЭКТЕСТ И АНАЛИТИКА -->
    <div id="pane-backtest" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <h2>Walk-Forward Бэктест на котировках Bybit V5 (Наивный Taker против Снайперского ИИ-комбайна)</h2>
        </div>
        <table>
          <thead>
            <tr><th>Метрика эффективности</th><th>Наивный индикаторный режим (Taker без фильтра издержек)</th><th>Снайперский ИИ-комбайн AItrade (Maker Limit + ML Cost-Aware &lambda;=2.0)</th></tr>
          </thead>
          <tbody>
            <tr><td>Общее число сделок (Turnover)</td><td>{n['total_trades']}</td><td><strong>{s['total_trades']} (снижение шумового оборота на {turnover_reduction}%)</strong></td></tr>
            <tr><td>Win Rate (% прибыльных сделок)</td><td>{n['win_rate_pct']}%</td><td><strong class="green" style="font-size:15px;">{s['win_rate_pct']}%</strong></td></tr>
            <tr><td>Profit Factor (Профит-фактор)</td><td>{n['profit_factor']}</td><td><strong class="green">{s['profit_factor']}</strong></td></tr>
            <tr><td>Чистая доходность после комиссий Bybit</td><td class="red">{n['net_return_pct']}%</td><td><strong class="green">+{s['net_return_pct']}%</strong></td></tr>
            <tr><td>Среднее математическое ожидание (R)</td><td class="red">{n['avg_r_multiple']}R</td><td><strong class="green">+{s['avg_r_multiple']}R</strong></td></tr>
            <tr><td>Максимальная просадка (Max Drawdown)</td><td class="red">{n['max_drawdown_pct']}%</td><td><strong>{s['max_drawdown_pct']}%</strong></td></tr>
            <tr><td>Sharpe Ratio / Deflated Sharpe (DSR)</td><td>{n['sharpe_ratio']} / {n['deflated_sharpe_ratio']}</td><td><strong class="amber">{s['sharpe_ratio']} / {s['deflated_sharpe_ratio']}</strong></td></tr>
          </tbody>
        </table>

        <div class="arch-grid">
          <div class="arch-box">
            <h4>1. Реальный стакан Bybit Unified Demo</h4>
            <p>Вместо пустого Testnet используется шлюз <code>api-demo.bybit.com</code> с живыми котировками и реальным стаканом Level-2 Bybit V5.</p>
          </div>
          <div class="arch-box">
            <h4>2. Исполнение лимитными ордерами (Maker 0.02%)</h4>
            <p>Снайперские входы от границ VWAP &plusmn;2&sigma;, 50% FVG и верифицированных плотностей экономят до 63% на комиссиях по сравнению с рыночными ордерами Taker (0.055%).</p>
          </div>
          <div class="arch-box">
            <h4>3. Защита от спуфинга (LBI) и рыночных шоков (DI)</h4>
            <p>4-ступенчатый фильтр верификации плотностей отсекает исчезающие плиты (Vanishing Walls), а индекс несходства DI &le; 1.0 блокирует торговлю в аномалиях.</p>
          </div>
          <div class="arch-box">
            <h4>4. Правило 5x дистанции до ликвидации</h4>
            <p>Кредитное плечо является расчетным следствием ширины стопа (макс. 4x), а цена ликвидации всегда находится минимум в 5 раз дальше стоп-лосса.</p>
          </div>
        </div>
      </div>
    </div>

    <!-- ВКЛАДКА 5: НАСТРОЙКИ СИСТЕМЫ -->
    <div id="pane-settings" class="tab-pane">
      <div class="panel">
        <div class="panel-header">
          <h2>Активная конфигурация риск-менеджмента, ML-ядра и шлюзов Bybit V5</h2>
        </div>
        <table>
          <thead><tr><th>Параметр</th><th>Текущее значение</th><th>Назначение в архитектуре AItrade</th></tr></thead>
          <tbody>
            <tr><td>Режим биржевого коннектора</td><td><strong>{ex_cfg.get('mode', 'demo').upper()}</strong></td><td>Торговый шлюз <code>{ex_cfg.get('demo_url')}</code> + рыночный поток <code>{ex_cfg.get('mainnet_url')}</code></td></tr>
            <tr><td>Комиссии Bybit Perpetuals</td><td>Maker: <strong>0.020%</strong> | Taker: <strong>0.055%</strong></td><td>Учитываются в Cost-Aware Execution Filter и расчете размера позиции</td></tr>
            <tr><td>Фиксированный риск на сделку</td><td><strong>{risk_cfg.get('risk_per_trade_pct', 0.005)*100:.2f}% от депозита</strong></td><td>Расчет объема позиции от ширины технического стоп-лосса (Leverage as an Output)</td></tr>
            <tr><td>Максимальное плечо портфеля</td><td><strong>&le; {risk_cfg.get('max_portfolio_leverage', 4.0)}x</strong></td><td>Жесткий потолок плеча для исключения каскадных маржин-коллов</td></tr>
            <tr><td>Запас до цены ликвидации</td><td><strong>&ge; {risk_cfg.get('min_liquidation_to_stop_multiple', 5.0)}x ширины стопа</strong></td><td>Правило пятикратной дистанции до ликвидации (5x Stop Distance Rule)</td></tr>
            <tr><td>Порог окупаемости издержек (&lambda;)</td><td><strong>&lambda; = {ml_cfg.get('cost_selectivity_lambda', 2.0)}</strong></td><td>Прогнозная доходность должна минимум вдвое перекрывать сумму комиссий и спреда</td></tr>
            <tr><td>Порог индекса несходства (DI)</td><td><strong>DI &le; {ml_cfg.get('di_threshold', 1.0)}</strong></td><td>Отсечение незнакомых рыночных режимов и новостных шоков по евклидовой дистанции</td></tr>
            <tr><td>Сглаживание микроструктуры стакана</td><td><strong>Savitzky-Golay (окно {ml_cfg.get('savgol_window', 7)}, полином {ml_cfg.get('savgol_polyorder', 2)})</strong></td><td>Подавление высокочастотного шума мерцающих заявок L2 OBI (arXiv 2506.05764v2)</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    function openTab(paneId, btnEl) {{
      document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
      document.getElementById(paneId).classList.add('active');
      btnEl.classList.add('active');
    }}

    function filterSignals() {{
      const q = (document.getElementById('sigSearch').value || '').toUpperCase().trim();
      document.querySelectorAll('.sig-card').forEach(card => {{
        const sym = (card.getAttribute('data-symbol') || '').toUpperCase();
        card.style.display = (!q || sym.includes(q)) ? 'block' : 'none';
      }});
    }}
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path

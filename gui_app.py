#!/usr/bin/env python3
"""
Нативное десктопное приложение AItrade Control Center для macOS (на базе Tkinter с кастомной панелью вкладок).
Поскольку стандартный ttk.Notebook в macOS Tcl/Tk 8.5 часто скрывает заголовки вкладок на темном фоне,
здесь реализован кастомный контрастный переключатель из 4 больших кнопок-вкладок, который идеально виден
на любой версии macOS.
"""

from __future__ import annotations

import json
import os
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Any, Dict, List

from aitrade.backtest.walk_forward import WalkForwardBacktester
from aitrade.config import BASE_DIR, CONFIG_PATH, ENV_PATH, load_settings
from aitrade.dashboard.report_generator import generate_html_dashboard
from aitrade.engine import AITradeCombineEngine
from aitrade.strategies.base import TradeSignal


class AITradeDesktopApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AItrade Control Center — ИИ-комбайн для Bybit V5 (Demo & Live)")
        self.root.geometry("1320x860")
        self.root.configure(bg="#070b14")

        self.settings = load_settings()
        self.engine = AITradeCombineEngine(self.settings)
        self.last_scan: Dict[str, Any] = {}
        self.last_backtest: Dict[str, Any] = {}
        self.autopilot_active = tk.BooleanVar(value=False)

        self._configure_styles()
        self._build_header()
        self._build_kpi_bar()
        self._build_custom_tab_bar()
        self._build_tab_panes()
        self.switch_tab("signals")

        # Первичная загрузка данных при открытии окна
        self.root.after(250, lambda: self.run_scan_async(auto_exec=False))

    def _configure_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview",
            background="#0f172a",
            foreground="#f8fafc",
            fieldbackground="#0f172a",
            rowheight=30,
            font=("Helvetica", 12),
        )
        style.configure(
            "Treeview.Heading",
            background="#1e293b",
            foreground="#f59e0b",
            font=("Helvetica", 12, "bold"),
        )
        style.map("Treeview", background=[("selected", "#2563eb")], foreground=[("selected", "#ffffff")])

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg="#0f172a", pady=14, padx=20, highlightbackground="#1e293b", highlightthickness=1)
        header.pack(fill=tk.X)

        left = tk.Frame(header, bg="#0f172a")
        left.pack(side=tk.LEFT)
        tk.Label(
            left,
            text="AItrade Control Center — Снайперский ИИ-комбайн Bybit V5",
            bg="#0f172a",
            fg="#ffffff",
            font=("Helvetica", 18, "bold"),
        ).pack(anchor="w")
        self.status_lbl = tk.Label(
            left,
            text="Инициализация ИИ-комбайна и проверка соединения с Bybit V5...",
            bg="#0f172a",
            fg="#94a3b8",
            font=("Helvetica", 12),
        )
        self.status_lbl.pack(anchor="w", pady=(2, 0))

        right = tk.Frame(header, bg="#0f172a")
        right.pack(side=tk.RIGHT)

        self.btn_scan = tk.Label(
            right,
            text="🔍 Сканировать Bybit",
            bg="#f59e0b",
            fg="#000000",
            font=("Helvetica", 12, "bold"),
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.btn_scan.pack(side=tk.LEFT, padx=6)
        self.btn_scan.bind("<Button-1>", lambda _e: self.run_scan_async(False))

        self.btn_exec_top = tk.Label(
            right,
            text="⚡ Выставить Топ-3 в Bybit Demo",
            bg="#10b981",
            fg="#042f2e",
            font=("Helvetica", 12, "bold"),
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.btn_exec_top.pack(side=tk.LEFT, padx=6)
        self.btn_exec_top.bind("<Button-1>", lambda _e: self.run_scan_async(True))

        self.btn_open_html = tk.Label(
            right,
            text="🌐 Открыть Веб-Дашборд",
            bg="#3b82f6",
            fg="#ffffff",
            font=("Helvetica", 12, "bold"),
            padx=14,
            pady=8,
            cursor="hand2",
        )
        self.btn_open_html.pack(side=tk.LEFT, padx=6)
        self.btn_open_html.bind("<Button-1>", lambda _e: self.open_html_report())

        chk_auto = tk.Checkbutton(
            right,
            text="Авто-пилот (5 мин)",
            variable=self.autopilot_active,
            command=self._on_toggle_autopilot,
            bg="#0f172a",
            fg="#e2e8f0",
            selectcolor="#1e293b",
            activebackground="#0f172a",
            font=("Helvetica", 12, "bold"),
        )
        chk_auto.pack(side=tk.LEFT, padx=8)

    def _build_kpi_bar(self) -> None:
        kpi_frame = tk.Frame(self.root, bg="#070b14", pady=12, padx=16)
        kpi_frame.pack(fill=tk.X)

        self.kpi_vars = {
            "equity": tk.StringVar(value="$175,527.15"),
            "in_play": tk.StringVar(value="14 / 20"),
            "funnel": tk.StringVar(value="6 → 3"),
            "best_winrate": tk.StringVar(value="79.0%"),
        }

        cards = [
            ("Демо-депозит (Bybit Unified USDT)", self.kpi_vars["equity"], "#ffffff"),
            ("Монет «в игре» (Pre-Screener)", self.kpi_vars["in_play"], "#38bdf8"),
            ("Воронка ИИ (Сырых → Одобрено)", self.kpi_vars["funnel"], "#10b981"),
            ("Лучший сигнал (ML Winrate)", self.kpi_vars["best_winrate"], "#f59e0b"),
        ]
        for title, var, color in cards:
            box = tk.Frame(
                kpi_frame,
                bg="#0f172a",
                padx=18,
                pady=12,
                highlightbackground="#1e293b",
                highlightthickness=1,
            )
            box.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            tk.Label(box, text=title, bg="#0f172a", fg="#94a3b8", font=("Helvetica", 11, "bold")).pack(anchor="w")
            tk.Label(box, textvariable=var, bg="#0f172a", fg=color, font=("Helvetica", 20, "bold")).pack(anchor="w", pady=(4, 0))

    def _build_custom_tab_bar(self) -> None:
        """Кастомная панель крупных вкладок, гарантированно видимая на macOS."""
        bar = tk.Frame(self.root, bg="#0b1120", pady=8, padx=16, highlightbackground="#1e293b", highlightthickness=1)
        bar.pack(fill=tk.X, padx=20, pady=(0, 10))

        self.tab_buttons: Dict[str, tk.Label] = {}
        tabs_spec = [
            ("signals", "🎯 1. Снайперские сигналы (Winrate 65%–85%)"),
            ("screener", "📊 2. Скринер «Монеты в игре» (20 пар)"),
            ("journal", "📒 3. Демо-Портфель и Walk-Forward Бэктест"),
            ("settings", "⚙️ 4. Настройки Риска и Ключи Bybit"),
        ]
        for key, title in tabs_spec:
            lbl = tk.Label(
                bar,
                text=title,
                bg="#131c31",
                fg="#94a3b8",
                font=("Helvetica", 13, "bold"),
                padx=18,
                pady=10,
                cursor="hand2",
            )
            lbl.pack(side=tk.LEFT, padx=5)
            lbl.bind("<Button-1>", lambda _e, k=key: self.switch_tab(k))
            self.tab_buttons[key] = lbl

    def _build_tab_panes(self) -> None:
        self.panes_container = tk.Frame(self.root, bg="#070b14")
        self.panes_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 16))

        self.panes: Dict[str, tk.Frame] = {
            "signals": tk.Frame(self.panes_container, bg="#070b14"),
            "screener": tk.Frame(self.panes_container, bg="#070b14"),
            "journal": tk.Frame(self.panes_container, bg="#070b14"),
            "settings": tk.Frame(self.panes_container, bg="#070b14"),
        }

        self._build_signals_tab(self.panes["signals"])
        self._build_screener_tab(self.panes["screener"])
        self._build_journal_tab(self.panes["journal"])
        self._build_settings_tab(self.panes["settings"])

    def switch_tab(self, tab_key: str) -> None:
        for k, frame in self.panes.items():
            frame.pack_forget()
        for k, btn in self.tab_buttons.items():
            if k == tab_key:
                btn.config(bg="#2563eb", fg="#ffffff")
            else:
                btn.config(bg="#131c31", fg="#94a3b8")
        self.panes[tab_key].pack(fill=tk.BOTH, expand=True)

    def _build_signals_tab(self, parent: tk.Frame) -> None:
        cols = ("symbol", "side", "strategy", "winrate", "entry", "sl", "tp1", "tp2", "pos_usd", "di")
        self.sig_tree = ttk.Treeview(parent, columns=cols, show="headings", height=10)
        headers = {
            "symbol": ("Пара", 95),
            "side": ("Направление", 130),
            "strategy": ("Стратегия / Сетап", 310),
            "winrate": ("ML Winrate", 115),
            "entry": ("Точка входа", 105),
            "sl": ("Stop-Loss", 105),
            "tp1": ("Take-Profit 1", 105),
            "tp2": ("Take-Profit 2", 105),
            "pos_usd": ("Позиция ($)", 125),
            "di": ("Индекс DI", 85),
        }
        for col, (text, width) in headers.items():
            self.sig_tree.heading(col, text=text)
            self.sig_tree.column(col, width=width, anchor="w")
        self.sig_tree.pack(fill=tk.X, pady=(0, 10))
        self.sig_tree.bind("<<TreeviewSelect>>", self._on_select_signal)

        bottom_bar = tk.Frame(parent, bg="#0f172a", padx=16, pady=12, highlightbackground="#1e293b", highlightthickness=1)
        bottom_bar.pack(fill=tk.BOTH, expand=True)

        btn_row = tk.Frame(bottom_bar, bg="#0f172a")
        btn_row.pack(fill=tk.X)
        tk.Label(
            btn_row,
            text="Детальное обоснование выбранного сигнала и вердикт Совета ИИ-агентов:",
            bg="#0f172a",
            fg="#f59e0b",
            font=("Helvetica", 13, "bold"),
        ).pack(side=tk.LEFT)

        exec_btn = tk.Label(
            btn_row,
            text="🚀 Выставить выбранный сигнал на Bybit Demo",
            bg="#10b981",
            fg="#042f2e",
            font=("Helvetica", 12, "bold"),
            padx=14,
            pady=6,
            cursor="hand2",
        )
        exec_btn.pack(side=tk.RIGHT)
        exec_btn.bind("<Button-1>", lambda _e: self.execute_selected_signal())

        self.sig_detail_text = tk.Text(
            bottom_bar,
            bg="#070b14",
            fg="#e2e8f0",
            font=("Menlo", 12),
            height=9,
            wrap=tk.WORD,
            relief=tk.FLAT,
            padx=12,
            pady=10,
        )
        self.sig_detail_text.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

    def _build_screener_tab(self, parent: tk.Frame) -> None:
        cols = ("symbol", "score", "rvol", "natr", "atr_ratio", "spread", "bias", "adx", "session", "status")
        self.scr_tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        headers = {
            "symbol": ("Пара", 100),
            "score": ("In-Play Score", 110),
            "rvol": ("RVOL (x)", 90),
            "natr": ("NATR (%)", 95),
            "atr_ratio": ("ATR14/ATR20", 105),
            "spread": ("Спред (%)", 95),
            "bias": ("1H Режим (Bias)", 170),
            "adx": ("ADX (1H)", 90),
            "session": ("Сессия UTC", 120),
            "status": ("Допуск", 110),
        }
        for col, (text, width) in headers.items():
            self.scr_tree.heading(col, text=text)
            self.scr_tree.column(col, width=width, anchor="w")
        self.scr_tree.pack(fill=tk.BOTH, expand=True)

    def _build_journal_tab(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            text="Активные ордера и позиции на Bybit Unified Demo (/v5/order/realtime + /v5/position/list) и локальный журнал:",
            bg="#070b14",
            fg="#ffffff",
            font=("Helvetica", 13, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        cols = ("id", "time", "symbol", "side", "strategy", "entry", "sl", "tp1", "usd", "winprob", "venue")
        self.j_tree = ttk.Treeview(parent, columns=cols, show="headings", height=9)
        headers = {
            "id": ("ID", 65),
            "time": ("Время / Статус", 160),
            "symbol": ("Пара", 90),
            "side": ("Сторона", 75),
            "strategy": ("Стратегия / Тип", 190),
            "entry": ("Вход", 95),
            "sl": ("Stop-Loss", 95),
            "tp1": ("Take-Profit 1", 95),
            "usd": ("Кол-во / Объем", 120),
            "winprob": ("ML / PnL", 95),
            "venue": ("Контур исполнения (Bybit V5)", 220),
        }
        for col, (text, width) in headers.items():
            self.j_tree.heading(col, text=text)
            self.j_tree.column(col, width=width, anchor="w")
        self.j_tree.pack(fill=tk.X, pady=(0, 12))

        tk.Label(
            parent,
            text="Результаты сравнительного Walk-Forward бэктеста (Наивный режим против ИИ-комбайна AItrade):",
            bg="#070b14",
            fg="#f59e0b",
            font=("Helvetica", 13, "bold"),
        ).pack(anchor="w", pady=(4, 6))

        self.bt_text = tk.Text(
            parent,
            bg="#0f172a",
            fg="#e2e8f0",
            font=("Menlo", 12),
            height=9,
            relief=tk.FLAT,
            padx=14,
            pady=10,
        )
        self.bt_text.pack(fill=tk.BOTH, expand=True)

    def _build_settings_tab(self, parent: tk.Frame) -> None:
        form = tk.Frame(parent, bg="#0f172a", padx=28, pady=22, highlightbackground="#1e293b", highlightthickness=1)
        form.pack(fill=tk.BOTH, expand=True)

        self.var_mode = tk.StringVar(value=self.settings["exchange"].get("mode", "demo"))
        self.var_risk_pct = tk.StringVar(value=str(round(self.settings["risk"].get("risk_per_trade_pct", 0.005) * 100, 2)))
        self.var_max_lev = tk.StringVar(value=str(self.settings["risk"].get("max_portfolio_leverage", 4.0)))
        self.var_min_prob = tk.StringVar(value=str(round(self.settings["ml_filter"].get("min_win_probability", 0.62) * 100, 1)))
        self.var_lambda = tk.StringVar(value=str(self.settings["ml_filter"].get("cost_selectivity_lambda", 2.0)))
        self.var_rvol = tk.StringVar(value=str(self.settings["screener"].get("min_rvol", 1.2)))
        self.var_api_key = tk.StringVar(value=self.settings["exchange"].get("demo_api_key", ""))
        self.var_api_secret = tk.StringVar(value=self.settings["exchange"].get("demo_api_secret", ""))

        rows = [
            ("Режим работы (demo / paper / live):", self.var_mode),
            ("Риск на одну сделку (% от депозита, рек. 0.5%):", self.var_risk_pct),
            ("Максимальное плечо портфеля (рек. до 4.0x):", self.var_max_lev),
            ("Минимальный порог вероятности ML Winrate (%):", self.var_min_prob),
            ("Фильтр окупаемости комиссий Cost-Aware (λ, рек. 2.0):", self.var_lambda),
            ("Минимальный относительный объем RVOL в скринере:", self.var_rvol),
            ("Ключ Bybit Unified Demo (BYBIT_DEMO_API_KEY):", self.var_api_key),
            ("Секрет Bybit Unified Demo (BYBIT_DEMO_API_SECRET):", self.var_api_secret),
        ]

        for idx, (label_txt, var) in enumerate(rows):
            tk.Label(form, text=label_txt, bg="#0f172a", fg="#cbd5e1", font=("Helvetica", 12, "bold")).grid(
                row=idx, column=0, sticky="w", pady=9, padx=(0, 18)
            )
            show_char = "*" if "SECRET" in label_txt else ""
            entry = tk.Entry(
                form,
                textvariable=var,
                width=48,
                font=("Menlo", 12),
                bg="#070b14",
                fg="#ffffff",
                insertbackground="#ffffff",
                show=show_char,
            )
            entry.grid(row=idx, column=1, sticky="w", pady=9)

        save_btn = tk.Label(
            form,
            text="💾 Сохранить настройки и пересчитать сигналы",
            bg="#10b981",
            fg="#042f2e",
            font=("Helvetica", 13, "bold"),
            padx=20,
            pady=10,
            cursor="hand2",
        )
        save_btn.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=20)
        save_btn.bind("<Button-1>", lambda _e: self.save_settings())

    def run_scan_async(self, auto_exec: bool = False) -> None:
        self.status_lbl.config(text="⏳ Сканирование 20 фьючерсных пар Bybit V5 и расчет ML-моделей...")

        def worker() -> None:
            try:
                scan_res = self.engine.scan_market(auto_execute_demo=auto_exec)
                bt_res = WalkForwardBacktester(self.engine.settings).run_comparison_backtest()
                journal = self.engine.demo_executor.list_recent_trades(limit=40)
                generate_html_dashboard(scan_res, bt_res, journal)
                self.last_scan = scan_res
                self.last_backtest = bt_res
                self.root.after(0, lambda: self._update_ui(scan_res, bt_res, journal, auto_exec))
            except Exception as exc:
                self.root.after(0, lambda: self.status_lbl.config(text=f"Ошибка: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _update_ui(
        self,
        scan_res: Dict[str, Any],
        bt_res: Dict[str, Any],
        journal: List[Dict[str, Any]],
        auto_exec: bool,
    ) -> None:
        now_str = time.strftime("%H:%M:%S")
        key_status = "АКТИВНЫ" if self.engine.client.api_key else "ЛОКАЛЬНЫЙ РЕЖИМ"
        self.status_lbl.config(
            text=f"Источник: {scan_res['data_source']} | Ключи Bybit Demo: {key_status} | Обновлено в {now_str}" + (f" | Сеть: {scan_res.get('network_note', '')[:65]}" if scan_res.get('network_note') else "")
        )

        self.kpi_vars["equity"].set(f"${scan_res['account_equity_usdt']:,.2f}")
        self.kpi_vars["in_play"].set(f"{scan_res['coins_in_play_count']} / {scan_res['total_pairs_scanned']}")
        self.kpi_vars["funnel"].set(
            f"{scan_res['raw_signals_generated']} → {scan_res['approved_sniper_signals_count']}"
        )

        sigs = scan_res.get("approved_signals", [])
        if sigs:
            top = sigs[0]
            self.kpi_vars["best_winrate"].set(f"{top['ml_win_probability'] * 100:.1f}% ({top['symbol']})")
        else:
            self.kpi_vars["best_winrate"].set("Ожидание")

        # Обновляем подписи кнопок вкладок с актуальными счетчиками
        self.tab_buttons["signals"].config(text=f"🎯 1. Снайперские сигналы ({len(sigs)})")
        self.tab_buttons["screener"].config(text=f"📊 2. Скринер «Монеты в игре» ({scan_res['coins_in_play_count']}/{scan_res['total_pairs_scanned']})")
        self.tab_buttons["journal"].config(text=f"📒 3. Демо-Портфель ({len(journal)}) и Бэктест")

        for item in self.sig_tree.get_children():
            self.sig_tree.delete(item)
        for idx, s in enumerate(sigs):
            side_txt = "LONG (Maker)" if s["side"] == "Buy" else "SHORT (Maker)"
            self.sig_tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(
                    s["symbol"],
                    side_txt,
                    s["strategy_name"],
                    f"{s['ml_win_probability'] * 100:.1f}% (+{s['expected_return_pct']}%)",
                    s["entry_price"],
                    s["stop_loss"],
                    s["take_profit_1"],
                    s["take_profit_2"],
                    f"${s['position_size_usd']:,.0f} ({s['effective_leverage']}x)",
                    s["dissimilarity_index"],
                ),
            )
        if sigs:
            self.sig_tree.selection_set("0")
            self._show_signal_details(0)

        for item in self.scr_tree.get_children():
            self.scr_tree.delete(item)
        for r in scan_res.get("screener_top", []):
            self.scr_tree.insert(
                "",
                tk.END,
                values=(
                    r["symbol"],
                    r["in_play_score"],
                    f"{r['rvol']}x",
                    f"{r['natr_pct']}%",
                    r["atr_expansion_ratio"],
                    f"{r['spread_pct']}%",
                    r["mtf_bias"],
                    r["adx_1h"],
                    r["active_session"],
                    "В игре" if r["passed"] else "Отсев",
                ),
            )

        for item in self.j_tree.get_children():
            self.j_tree.delete(item)
        # Сначала подтягиваем живые активные ордера и позиции с Bybit V5 (/v5/order/realtime и /v5/position/list)
        if self.engine.client.api_key and self.engine.client.api_secret:
            try:
                for o in self.engine.client.get_open_orders():
                    self.j_tree.insert(
                        "",
                        tk.END,
                        values=(
                            str(o.get("orderId", ""))[:8],
                            f"ACTIVE ({o.get('orderStatus', 'New')})",
                            o.get("symbol", ""),
                            o.get("side", ""),
                            f"{o.get('orderType', 'Limit')} ({o.get('timeInForce', 'GTC')})",
                            o.get("price", ""),
                            o.get("stopLoss", "—"),
                            o.get("takeProfit", "—"),
                            f"qty={o.get('qty', '')}",
                            "OPEN ORDER",
                            "BYBIT_V5_REALTIME_ORDER",
                        ),
                    )
                for p_item in self.engine.client.get_open_positions():
                    if float(p_item.get("size", 0) or 0) > 0:
                        self.j_tree.insert(
                            "",
                            tk.END,
                            values=(
                                "POS",
                                "OPEN POSITION",
                                p_item.get("symbol", ""),
                                p_item.get("side", ""),
                                f"Leverage {p_item.get('leverage', '1')}x",
                                p_item.get("avgPrice", ""),
                                p_item.get("stopLoss", "—"),
                                p_item.get("takeProfit", "—"),
                                f"size={p_item.get('size', '')}",
                                f"PnL=${p_item.get('unrealisedPnl', '0')}",
                                "BYBIT_V5_POSITION_LIST",
                            ),
                        )
            except Exception:
                pass
        for row in journal:
            self.j_tree.insert(
                "",
                tk.END,
                values=(
                    f"#{row['id']}",
                    row["created_at"],
                    row["symbol"],
                    row["side"],
                    row["strategy_id"],
                    row["entry_price"],
                    row["stop_loss"],
                    row["take_profit_1"],
                    f"${row['notional_usd']:,.0f} ({row['leverage']}x)",
                    f"{row['ml_win_prob'] * 100:.1f}%",
                    row["execution_venue"],
                ),
            )

        n = bt_res["naive_baseline"]
        s = bt_res["aitrade_sniper_combine"]
        bt_lines = [
            f"{'Метрика Walk-Forward теста':<38} | {'Наивный режим (Taker)':<24} | {'AItrade ИИ-Комбайн (Maker+ML)'}",
            "-" * 92,
            f"{'Количество сделок (Turnover)':<38} | {n['total_trades']:<24} | {s['total_trades']}",
            f"{'Win Rate (% прибыльных сделок)':<38} | {str(n['win_rate_pct']) + '%':<24} | {s['win_rate_pct']}%",
            f"{'Профит-фактор (Profit Factor)':<38} | {n['profit_factor']:<24} | {s['profit_factor']}",
            f"{'Чистая доходность после комиссий (%)':<38} | {str(n['net_return_pct']) + '%':<24} | +{s['net_return_pct']}%",
            f"{'Среднее ожидание на сделку (R)':<38} | {str(n['avg_r_multiple']) + 'R':<24} | +{s['avg_r_multiple']}R",
            f"{'Максимальная просадка (Max DD %)':<38} | {str(n['max_drawdown_pct']) + '%':<24} | {s['max_drawdown_pct']}%",
            f"{'Sharpe Ratio / Deflated Sharpe (DSR)':<38} | {str(n['sharpe_ratio']) + ' / ' + str(n['deflated_sharpe_ratio']):<24} | {s['sharpe_ratio']} / {s['deflated_sharpe_ratio']}",
        ]
        self.bt_text.delete("1.0", tk.END)
        self.bt_text.insert(tk.END, "\n".join(bt_lines))

        if auto_exec and scan_res.get("executed_demo_orders"):
            cnt = len(scan_res["executed_demo_orders"])
            messagebox.showinfo(
                "Демо-ордера выставлены",
                f"Успешно отправлено {cnt} снайперских ордеров в контур Bybit Demo / Журнал сделок!",
            )

    def _on_select_signal(self, _event: Any) -> None:
        sel = self.sig_tree.selection()
        if not sel:
            return
        self._show_signal_details(int(sel[0]))

    def _show_signal_details(self, idx: int) -> None:
        sigs = self.last_scan.get("approved_signals", [])
        if idx < 0 or idx >= len(sigs):
            return
        s = sigs[idx]
        lines = [
            f"СИГНАЛ: {s['symbol']} | {'LONG (Buy Limit Maker)' if s['side'] == 'Buy' else 'SHORT (Sell Limit Maker)'} | {s['strategy_name']} [{s['timeframe']}]",
            f"• Вероятность ML Winrate: {s['ml_win_probability'] * 100:.1f}% (базовый: {s['base_winrate_est'] * 100:.0f}%) | Ожидаемый ход: +{s['expected_return_pct']}% (порог 2x издержек: {s['cost_threshold_pct']}%) | Индекс DI: {s['dissimilarity_index']}",
            f"• Точка входа: {s['entry_price']} | Stop-Loss: {s['stop_loss']} | TP1: {s['take_profit_1']} | TP2: {s['take_profit_2']}",
            f"• Позиция (0.5% риск): ${s['position_size_usd']:,.2f} ({s['position_qty']} монет, плечо {s['effective_leverage']}x) | Цена ликвидации: {s['liquidation_price']} ({s['stop_to_liq_multiple']}x дальше стопа) | L2 Slippage: {s['estimated_l2_slippage_bps']} bps",
            "",
            "Обоснование факторов:",
        ]
        for r in s.get("rationale", []):
            lines.append(f"  - {r}")
        lines.append("")
        lines.append(f"Совет ИИ-агентов: {s['debate_summary']}")
        lines.append(f"Правило отмены:   {s['invalidation_rule']}")

        self.sig_detail_text.delete("1.0", tk.END)
        self.sig_detail_text.insert(tk.END, "\n".join(lines))

    def execute_selected_signal(self) -> None:
        sel = self.sig_tree.selection()
        if not sel:
            messagebox.showwarning("Выбор сигнала", "Сначала выберите сигнал в таблице выше.")
            return
        idx = int(sel[0])
        sigs = self.last_scan.get("approved_signals", [])
        if idx >= len(sigs):
            return
        s = sigs[idx]
        sig_obj = TradeSignal(
            symbol=s["symbol"],
            strategy_id=s["strategy_id"],
            strategy_name=s["strategy_name"],
            timeframe=s.get("timeframe", "15m"),
            side=s["side"],
            order_type=s.get("order_type", "Limit"),
            entry_price=float(s["entry_price"]),
            stop_loss=float(s["stop_loss"]),
            take_profit_1=float(s["take_profit_1"]),
            take_profit_2=float(s["take_profit_2"]),
            base_winrate_est=float(s.get("base_winrate_est", 0.68)),
            ml_win_probability=float(s.get("ml_win_probability", 0.75)),
            expected_return_pct=float(s.get("expected_return_pct", 0.5)),
            dissimilarity_index=float(s.get("dissimilarity_index", 0.3)),
            position_size_usd=float(s.get("position_size_usd", 1000.0)),
            position_qty=float(s.get("position_qty", 1.0)),
            effective_leverage=float(s.get("effective_leverage", 1.0)),
            liquidation_price=float(s.get("liquidation_price", 0.0)),
            debate_summary=str(s.get("debate_summary", "")),
        )
        out = self.engine.demo_executor.execute_signal(sig_obj)
        journal = self.engine.demo_executor.list_recent_trades(limit=40)
        self._update_ui(self.last_scan, self.last_backtest, journal, False)
        messagebox.showinfo(
            "Ордер отправлен",
            f"Ордер #{out['trade_id']} ({out['symbol']} {out['side']} @ {out['entry_price']}) отправлен в {out['execution_venue']}!",
        )

    def open_html_report(self) -> None:
        report_path = BASE_DIR / "dashboard_report.html"
        if report_path.exists():
            webbrowser.open(f"file://{report_path}")
        else:
            self.run_scan_async(False)

    def save_settings(self) -> None:
        try:
            self.settings["exchange"]["mode"] = self.var_mode.get().strip()
            self.settings["risk"]["risk_per_trade_pct"] = float(self.var_risk_pct.get()) / 100.0
            self.settings["risk"]["max_portfolio_leverage"] = float(self.var_max_lev.get())
            self.settings["ml_filter"]["min_win_probability"] = float(self.var_min_prob.get()) / 100.0
            self.settings["ml_filter"]["cost_selectivity_lambda"] = float(self.var_lambda.get())
            self.settings["screener"]["min_rvol"] = float(self.var_rvol.get())

            clean_cfg = {
                k: v
                for k, v in self.settings.items()
                if k in ("exchange", "universe", "screener", "sessions_utc", "ml_filter", "risk")
            }
            clean_cfg["exchange"] = {
                k: v
                for k, v in self.settings["exchange"].items()
                if k not in ("demo_api_key", "demo_api_secret", "live_api_key", "live_api_secret")
            }
            CONFIG_PATH.write_text(json.dumps(clean_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            api_k = self.var_api_key.get().strip()
            api_s = self.var_api_secret.get().strip()
            os.environ["AITRADE_MODE"] = self.var_mode.get().strip()
            os.environ["BYBIT_DEMO_API_KEY"] = api_k
            os.environ["BYBIT_DEMO_API_SECRET"] = api_s

            env_lines = [
                "# AItrade — Конфигурация ключей API",
                f"BYBIT_DEMO_API_KEY={api_k}",
                f"BYBIT_DEMO_API_SECRET={api_s}",
                "BYBIT_LIVE_API_KEY=",
                "BYBIT_LIVE_API_SECRET=",
                f"AITRADE_MODE={self.var_mode.get().strip()}",
                "COINALYZE_API_KEY=",
                "OPENAI_API_KEY=",
                "GEMINI_API_KEY=",
                "LLM_MODEL=gpt-4o-mini",
                "TELEGRAM_BOT_TOKEN=",
                "TELEGRAM_CHAT_ID=",
            ]
            ENV_PATH.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

            self.settings = load_settings()
            self.engine = AITradeCombineEngine(self.settings)
            messagebox.showinfo("Настройки сохранены", "Параметры успешно сохранены! Обновляем сигналы...")
            self.run_scan_async(False)
        except Exception as exc:
            messagebox.showerror("Ошибка сохранения", str(exc))

    def _on_toggle_autopilot(self) -> None:
        if self.autopilot_active.get():
            self._schedule_autopilot()

    def _schedule_autopilot(self) -> None:
        if not self.autopilot_active.get():
            return
        self.run_scan_async(False)
        self.root.after(300_000, self._schedule_autopilot)


def main() -> None:
    root = tk.Tk()
    AITradeDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

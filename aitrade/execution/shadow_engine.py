# -*- coding: utf-8 -*-
"""
Live Shadow Paper Engine — безконфликтный полигон для параллельного тестирования
любого числа стратегий (10–50+), каждая на своём виртуальном счёте $1,000.

Ключевые свойства:
- Полная независимость стратегий: 10 разных стратегий могут одновременно открыть
  виртуальную сделку по одной и той же монете (SOLUSDT и т.д.) без конфликта на бирже.
- Реалистичное исполнение: вход и выход по живому WebSocket bid/ask + комиссия taker
  (0.055% на вход + 0.055% на выход) + 2 bps буфер проскальзывания.
- Долговременное хранение в SQLite (data/shadow_lab.sqlite3).
"""

from __future__ import annotations

import datetime
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_HOLD_MINUTES = {
    "T1_DONCHIAN_TSMOM": 14400,
    "T3_KELTNER_TREND_4H": 10080,
    "T2_RS_BTC_BREAKOUT": 2880,
    "C2_MTF_COMBO_BREAKOUT": 2160,
    "C3_ENSEMBLE_VOTE_2OF3": 1440,
    "B1_SQUEEZE_BREAKOUT": 1080,
    "B2_DONCHIAN_INTRADAY": 1080,
    "B7_FUNDING_SQUEEZE_MOMENTUM": 1080,
    "S1_TREND_RETEST": 1080,
    "B5_OI_TREND_EXPANSION": 720,
    "B6_EMA_PULLBACK_TREND": 720,
    "B3_VWAP_MOMENTUM_IMPULSE": 480,
    "B4_ORB_SESSION_BREAKOUT": 480,
    "AUTOTRADE_MOMENTUM_FUNDING": 480,
    "S3_VWAP_RANGE": 240,
}


class LiveShadowEngine:
    """Ведёт виртуальные $1,000-подсчета для каждой стратегии по живым ценам WebSocket."""

    POD_CAPITAL_USD = 1000.0
    RISK_PER_TRADE_USD = 15.0  # 1.5% от $1,000 на сделку — одинаковый масштаб для всех стратегий
    MAX_POD_LEVERAGE = 3.0
    TAKER_FEE_RATE = 0.00055
    SLIPPAGE_RATE = 0.0002

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_ts REAL NOT NULL,
                    closed_ts REAL DEFAULT 0,
                    max_hold_min INTEGER NOT NULL,
                    signal_entry REAL NOT NULL,
                    entry_px REAL NOT NULL,
                    sl_px REAL NOT NULL,
                    tp_px REAL NOT NULL,
                    exit_px REAL DEFAULT 0,
                    qty REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    risk_usd REAL NOT NULL,
                    fees_usd REAL DEFAULT 0,
                    pnl_usd REAL DEFAULT 0,
                    realized_r REAL DEFAULT 0,
                    exit_reason TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status);
                CREATE INDEX IF NOT EXISTS idx_shadow_strat ON shadow_trades(strategy_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=10)
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def record_signals(
        self,
        signals: List[Dict[str, Any]],
        bbo_map: Dict[str, Tuple[float, float]],
        now_ts: Optional[float] = None,
    ) -> int:
        """Открывает виртуальные сделки по всем сигналам (не более 1 открытой сделки на пару внутри одной стратегии)."""
        ts = float(now_ts if now_ts is not None else time.time())
        opened_count = 0
        with self._lock, self._connect() as conn:
            open_keys = {
                (r[0], r[1])
                for r in conn.execute("SELECT strategy_id, symbol FROM shadow_trades WHERE status='OPEN'").fetchall()
            }
            for sig in signals:
                strat_id = str(sig.get("strategy_id") or "").strip()
                sym = str(sig.get("symbol") or "").strip()
                side = str(sig.get("side") or "").strip()
                if not strat_id or not sym or side not in ("Buy", "Sell"):
                    continue
                if (strat_id, sym) in open_keys:
                    continue

                sig_entry = float(sig.get("entry_price") or 0.0)
                sl_px = float(sig.get("stop_loss") or 0.0)
                tp_px = float(sig.get("take_profit") or sig.get("take_profit_2") or sig.get("take_profit_1") or 0.0)
                if sig_entry <= 0 or sl_px <= 0 or tp_px <= 0:
                    continue

                bid, ask = bbo_map.get(sym, (0.0, 0.0))
                if bid > 0 and ask > 0:
                    raw_fill = ask if side == "Buy" else bid
                else:
                    raw_fill = sig_entry
                entry_px = raw_fill * (1.0 + self.SLIPPAGE_RATE) if side == "Buy" else raw_fill * (1.0 - self.SLIPPAGE_RATE)

                # Проверка валидности SL/TP относительно фактического входа
                if side == "Buy" and not (sl_px < entry_px < tp_px):
                    continue
                if side == "Sell" and not (tp_px < entry_px < sl_px):
                    continue

                stop_dist = abs(entry_px - sl_px)
                if stop_dist <= 1e-9:
                    continue
                max_notional = self.POD_CAPITAL_USD * self.MAX_POD_LEVERAGE
                qty = min(self.RISK_PER_TRADE_USD / stop_dist, max_notional / entry_px)
                notional = qty * entry_px
                if notional < 10.0:
                    continue
                planned_risk = qty * stop_dist
                hold_min = int(DEFAULT_HOLD_MINUTES.get(strat_id, 720))

                tid = "sh-" + uuid.uuid4().hex[:16]
                conn.execute(
                    """
                    INSERT INTO shadow_trades
                    (id, strategy_id, symbol, side, status, opened_ts, max_hold_min, signal_entry, entry_px, sl_px, tp_px, qty, notional_usd, risk_usd)
                    VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tid, strat_id, sym, side, ts, hold_min, sig_entry, entry_px, sl_px, tp_px, qty, notional, planned_risk),
                )
                open_keys.add((strat_id, sym))
                opened_count += 1
        return opened_count

    def update_open_positions(
        self,
        bbo_map: Dict[str, Tuple[float, float]],
        now_ts: Optional[float] = None,
    ) -> int:
        """Проверяет все открытые shadow-позиции против живого bid/ask на SL, TP и Time-Stop."""
        ts = float(now_ts if now_ts is not None else time.time())
        closed_count = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, symbol, side, opened_ts, max_hold_min, entry_px, sl_px, tp_px, qty, risk_usd "
                "FROM shadow_trades WHERE status='OPEN'"
            ).fetchall()
            for tid, sym, side, opened_ts, max_hold_min, entry_px, sl_px, tp_px, qty, risk_usd in rows:
                bid, ask = bbo_map.get(sym, (0.0, 0.0))
                if bid <= 0 or ask <= 0:
                    continue
                # Для выхода из Long продаём по bid; для выхода из Short покупаем по ask
                exit_mkt = bid if side == "Buy" else ask
                reason = ""
                exit_px = 0.0
                if side == "Buy":
                    if exit_mkt <= sl_px:
                        reason = "SL"
                        exit_px = min(exit_mkt, sl_px) * (1.0 - self.SLIPPAGE_RATE)
                    elif exit_mkt >= tp_px:
                        reason = "TP"
                        exit_px = max(exit_mkt, tp_px)
                else:
                    if exit_mkt >= sl_px:
                        reason = "SL"
                        exit_px = max(exit_mkt, sl_px) * (1.0 + self.SLIPPAGE_RATE)
                    elif exit_mkt <= tp_px:
                        reason = "TP"
                        exit_px = min(exit_mkt, tp_px)

                if not reason and max_hold_min > 0 and (ts - opened_ts) >= max_hold_min * 60.0:
                    reason = "TIME_STOP"
                    exit_px = exit_mkt * (1.0 - self.SLIPPAGE_RATE if side == "Buy" else 1.0 + self.SLIPPAGE_RATE)

                if not reason:
                    continue

                gross_pnl = (exit_px - entry_px) * qty if side == "Buy" else (entry_px - exit_px) * qty
                fees = (entry_px * qty + exit_px * qty) * self.TAKER_FEE_RATE
                net_pnl = round(gross_pnl - fees, 4)
                realized_r = round(net_pnl / max(risk_usd, 1e-6), 3)
                conn.execute(
                    """
                    UPDATE shadow_trades
                    SET status='CLOSED', closed_ts=?, exit_px=?, fees_usd=?, pnl_usd=?, realized_r=?, exit_reason=?
                    WHERE id=?
                    """,
                    (ts, exit_px, round(fees, 4), net_pnl, realized_r, reason, tid),
                )
                closed_count += 1
        return closed_count

    def get_summary(self, bbo_map: Optional[Dict[str, Tuple[float, float]]] = None) -> Dict[str, Any]:
        bbo = bbo_map or {}
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, strategy_id, symbol, side, status, opened_ts, closed_ts, entry_px, sl_px, tp_px, exit_px, qty, risk_usd, fees_usd, pnl_usd, realized_r, exit_reason "
                "FROM shadow_trades ORDER BY opened_ts ASC"
            ).fetchall()

        by_strat: Dict[str, Dict[str, Any]] = {}
        open_list: List[Dict[str, Any]] = []
        closed_list: List[Dict[str, Any]] = []

        for r in rows:
            tid, s_id, sym, side, status, o_ts, c_ts, e_px, sl_px, tp_px, x_px, qty, risk_usd, fees, pnl, r_mult, reason = r
            st = by_strat.setdefault(
                s_id,
                {
                    "strategy_id": s_id,
                    "pod_capital_usd": self.POD_CAPITAL_USD,
                    "open_trades": 0,
                    "closed_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "net_pnl_usd": 0.0,
                    "unrealized_pnl_usd": 0.0,
                    "r_sum": 0.0,
                    "hold_h_sum": 0.0,
                    "eq_peak": 0.0,
                    "eq_cur": 0.0,
                    "max_dd_usd": 0.0,
                },
            )
            if status == "OPEN":
                st["open_trades"] += 1
                bid, ask = bbo.get(sym, (0.0, 0.0))
                mark = bid if (side == "Buy" and bid > 0) else (ask if (side == "Sell" and ask > 0) else e_px)
                upnl = round(((mark - e_px) * qty if side == "Buy" else (e_px - mark) * qty), 2)
                st["unrealized_pnl_usd"] = round(st["unrealized_pnl_usd"] + upnl, 2)
                open_list.append(
                    {
                        "id": tid,
                        "strategy_id": s_id,
                        "symbol": sym,
                        "side": side,
                        "entry_px": round(e_px, 6),
                        "mark_px": round(mark, 6),
                        "sl_px": round(sl_px, 6),
                        "tp_px": round(tp_px, 6),
                        "upnl_usd": upnl,
                        "age_hours": round(max(0.0, (time.time() - o_ts) / 3600.0), 1),
                    }
                )
            else:
                st["closed_trades"] += 1
                if pnl > 0:
                    st["wins"] += 1
                    st["gross_profit"] += pnl
                elif pnl < 0:
                    st["losses"] += 1
                    st["gross_loss"] += abs(pnl)
                st["net_pnl_usd"] += pnl
                st["r_sum"] += r_mult
                st["hold_h_sum"] += max(0.0, (c_ts - o_ts) / 3600.0)
                st["eq_cur"] += pnl
                if st["eq_cur"] > st["eq_peak"]:
                    st["eq_peak"] = st["eq_cur"]
                dd = st["eq_peak"] - st["eq_cur"]
                if dd > st["max_dd_usd"]:
                    st["max_dd_usd"] = dd
                closed_list.append(
                    {
                        "id": tid,
                        "strategy_id": s_id,
                        "symbol": sym,
                        "side": side,
                        "entry_px": round(e_px, 6),
                        "exit_px": round(x_px, 6),
                        "pnl_usd": round(pnl, 2),
                        "realized_r": round(r_mult, 2),
                        "exit_reason": reason,
                        "closed_at": datetime.datetime.fromtimestamp(c_ts, tz=datetime.timezone.utc).strftime("%m-%d %H:%M"),
                    }
                )

        leaderboard: List[Dict[str, Any]] = []
        for s_id, st in by_strat.items():
            n = st["closed_trades"]
            pf = round(st["gross_profit"] / st["gross_loss"], 2) if st["gross_loss"] > 0 else (99.0 if st["gross_profit"] > 0 else None)
            wr = round((st["wins"] / n) * 100.0, 1) if n > 0 else 0.0
            leaderboard.append(
                {
                    "strategy_id": s_id,
                    "pod_equity_usd": round(self.POD_CAPITAL_USD + st["net_pnl_usd"] + st["unrealized_pnl_usd"], 2),
                    "open_trades": st["open_trades"],
                    "closed_trades": n,
                    "win_rate_pct": wr,
                    "profit_factor": pf,
                    "net_pnl_usd": round(st["net_pnl_usd"], 2),
                    "unrealized_pnl_usd": round(st["unrealized_pnl_usd"], 2),
                    "avg_r": round(st["r_sum"] / n, 3) if n > 0 else None,
                    "avg_hold_hours": round(st["hold_h_sum"] / n, 1) if n > 0 else None,
                    "max_dd_usd": round(st["max_dd_usd"], 2),
                }
            )
        leaderboard.sort(key=lambda x: (x["net_pnl_usd"] + x["unrealized_pnl_usd"], x["closed_trades"]), reverse=True)

        return {
            "enabled": True,
            "pod_capital_usd": self.POD_CAPITAL_USD,
            "risk_per_trade_usd": self.RISK_PER_TRADE_USD,
            "total_open": len(open_list),
            "total_closed": len(closed_list),
            "leaderboard": leaderboard,
            "open_positions": open_list[-30:][::-1],
            "recent_closed": closed_list[-30:][::-1],
        }

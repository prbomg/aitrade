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

from contextlib import closing, contextmanager
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
        self._closed_cache_ver: Optional[Tuple[int, int]] = None
        self._closed_ver: int = 0
        self._closed_cache: Optional[Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]] = None
        with self._connect() as conn:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
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
                    exit_reason TEXT DEFAULT '',
                    signal_id TEXT DEFAULT '',
                    signal_created_ms INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shadow_signal_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    signal_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    shadow_trade_id TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_status ON shadow_trades(status);
                CREATE INDEX IF NOT EXISTS idx_shadow_strat ON shadow_trades(strategy_id);
                CREATE INDEX IF NOT EXISTS idx_shadow_dec_sig ON shadow_signal_decisions(signal_id);
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)").fetchall()}
            if "signal_id" not in cols:
                conn.execute("ALTER TABLE shadow_trades ADD COLUMN signal_id TEXT DEFAULT ''")
            if "signal_created_ms" not in cols:
                conn.execute("ALTER TABLE shadow_trades ADD COLUMN signal_created_ms INTEGER DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_signal_id ON shadow_trades(signal_id)")

    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        try:
            c.execute("PRAGMA busy_timeout=10000")
            with c:
                yield c
        finally:
            c.close()

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
            def _log_decision(sig_id: str, s_id: str, sym_s: str, side_s: str, decision: str, reason: str = "", trade_id: str = "") -> None:
                if not sig_id:
                    return
                conn.execute(
                    "INSERT INTO shadow_signal_decisions (ts, signal_id, strategy_id, symbol, side, decision, reason, shadow_trade_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, sig_id, s_id, sym_s, side_s, decision, reason[:160], trade_id),
                )

            for sig in signals:
                strat_id = str(sig.get("strategy_id") or "").strip()
                sym = str(sig.get("symbol") or "").strip()
                side = str(sig.get("side") or "").strip()
                sig_id = str(sig.get("signal_id") or "").strip()
                sig_created_ms = int(sig.get("signal_created_ms") or int(ts * 1000))
                if not strat_id or not sym or side not in ("Buy", "Sell"):
                    continue
                if (strat_id, sym) in open_keys:
                    _log_decision(sig_id, strat_id, sym, side, "SKIPPED", "Позиция по этой стратегии и монете уже открыта в Shadow")
                    continue

                sig_entry = float(sig.get("entry_price") or 0.0)
                sl_px = float(sig.get("stop_loss") or 0.0)
                tp_px = float(sig.get("take_profit") or sig.get("take_profit_2") or sig.get("take_profit_1") or 0.0)
                if sig_entry <= 0 or sl_px <= 0 or tp_px <= 0:
                    _log_decision(sig_id, strat_id, sym, side, "SKIPPED", "Некорректные уровни entry/SL/TP")
                    continue

                bid, ask = bbo_map.get(sym, (0.0, 0.0))
                if bid > 0 and ask > 0:
                    raw_fill = ask if side == "Buy" else bid
                else:
                    raw_fill = sig_entry
                entry_px = raw_fill * (1.0 + self.SLIPPAGE_RATE) if side == "Buy" else raw_fill * (1.0 - self.SLIPPAGE_RATE)

                # Проверка валидности SL/TP относительно фактического входа
                if side == "Buy" and not (sl_px < entry_px < tp_px):
                    _log_decision(sig_id, strat_id, sym, side, "SKIPPED", "Цена входа вышла за пределы SL/TP (Buy)")
                    continue
                if side == "Sell" and not (tp_px < entry_px < sl_px):
                    _log_decision(sig_id, strat_id, sym, side, "SKIPPED", "Цена входа вышла за пределы SL/TP (Sell)")
                    continue

                stop_dist = abs(entry_px - sl_px)
                if stop_dist <= 1e-9:
                    _log_decision(sig_id, strat_id, sym, side, "SKIPPED", "Нулевая дистанция стопа")
                    continue
                max_notional = self.POD_CAPITAL_USD * self.MAX_POD_LEVERAGE
                qty = min(self.RISK_PER_TRADE_USD / stop_dist, max_notional / entry_px)
                notional = qty * entry_px
                if notional < 10.0:
                    _log_decision(sig_id, strat_id, sym, side, "SKIPPED", "Номинал ниже минимума $10")
                    continue
                planned_risk = qty * stop_dist
                hold_min = int(DEFAULT_HOLD_MINUTES.get(strat_id, 720))

                tid = "sh-" + uuid.uuid4().hex[:16]
                conn.execute(
                    """
                    INSERT INTO shadow_trades
                    (id, strategy_id, symbol, side, status, opened_ts, max_hold_min, signal_entry, entry_px, sl_px, tp_px, qty, notional_usd, risk_usd, signal_id, signal_created_ms)
                    VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (tid, strat_id, sym, side, ts, hold_min, sig_entry, entry_px, sl_px, tp_px, qty, notional, planned_risk, sig_id, sig_created_ms),
                )
                _log_decision(sig_id, strat_id, sym, side, "ENTERED", "", tid)
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
            if closed_count > 0:
                self._bump_db_revision(conn)
                self._closed_ver += 1
        return closed_count

    @staticmethod
    def _bump_db_revision(conn: sqlite3.Connection) -> int:
        cur_ver = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0) + 1
        conn.execute(f"PRAGMA user_version = {cur_ver}")
        return cur_ver

    @staticmethod
    def _read_db_revision(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)

    def prune_old_decisions(self, max_age_days: int = 30) -> int:
        """Удаляет устаревшие записи из shadow_signal_decisions для контроля размера SQLite."""
        cutoff = time.time() - max(1, int(max_age_days)) * 86400.0
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM shadow_signal_decisions WHERE ts < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def backup_to(self, backup_dir: Path, keep: int = 7) -> Path:
        """Создаёт консистентную резервную копию shadow_lab.sqlite3 и хранит последние keep дней."""
        bdir = Path(backup_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        dest = bdir / f"{self.db_path.stem}-{day}{self.db_path.suffix}"
        with self._lock, self._connect() as src, closing(sqlite3.connect(dest)) as dst:
            with dst:
                src.backup(dst)
        files = sorted(bdir.glob(f"{self.db_path.stem}-*{self.db_path.suffix}"))
        for old in (files[:-keep] if len(files) > keep else []):
            old.unlink(missing_ok=True)
        return dest

    def _load_closed_agg_locked(self, conn: sqlite3.Connection) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
        cache_key = (self._closed_ver, self._read_db_revision(conn))
        if self._closed_cache is not None and self._closed_cache_ver == cache_key:
            return self._closed_cache
        rows = conn.execute(
            "SELECT id, strategy_id, symbol, side, opened_ts, closed_ts, entry_px, exit_px, pnl_usd, realized_r, exit_reason, signal_id, signal_created_ms "
            "FROM shadow_trades WHERE status='CLOSED' ORDER BY closed_ts ASC, opened_ts ASC"
        ).fetchall()
        by_strat: Dict[str, Dict[str, Any]] = {}
        closed_list: List[Dict[str, Any]] = []
        for r in rows:
            tid, s_id, sym, side, o_ts, c_ts, e_px, x_px, pnl, r_mult, reason, sig_id, sig_c_ms = r
            st = by_strat.setdefault(
                s_id,
                {
                    "strategy_id": s_id,
                    "pod_capital_usd": self.POD_CAPITAL_USD,
                    "closed_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "gross_profit": 0.0,
                    "gross_loss": 0.0,
                    "net_pnl_usd": 0.0,
                    "r_sum": 0.0,
                    "hold_h_sum": 0.0,
                    "eq_peak": 0.0,
                    "eq_cur": 0.0,
                    "max_dd_usd": 0.0,
                },
            )
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
                    "signal_id": sig_id or "",
                    "signal_created_ms": int(sig_c_ms or 0),
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
        self._closed_cache = (by_strat, closed_list)
        self._closed_cache_ver = cache_key
        return self._closed_cache

    def get_summary(self, bbo_map: Optional[Dict[str, Tuple[float, float]]] = None) -> Dict[str, Any]:
        bbo = bbo_map or {}
        with self._lock, self._connect() as conn:
            closed_by_strat, closed_list = self._load_closed_agg_locked(conn)
            open_rows = conn.execute(
                "SELECT id, strategy_id, symbol, side, opened_ts, entry_px, sl_px, tp_px, qty, signal_id, signal_created_ms "
                "FROM shadow_trades WHERE status='OPEN' ORDER BY opened_ts ASC"
            ).fetchall()

        by_strat: Dict[str, Dict[str, Any]] = {
            k: dict(v, open_trades=0, unrealized_pnl_usd=0.0) for k, v in closed_by_strat.items()
        }
        open_list: List[Dict[str, Any]] = []

        for r in open_rows:
            tid, s_id, sym, side, o_ts, e_px, sl_px, tp_px, qty, sig_id, sig_c_ms = r
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
            st["open_trades"] += 1
            bid, ask = bbo.get(sym, (0.0, 0.0))
            mark = bid if (side == "Buy" and bid > 0) else (ask if (side == "Sell" and ask > 0) else e_px)
            upnl = round(((mark - e_px) * qty if side == "Buy" else (e_px - mark) * qty), 2)
            st["unrealized_pnl_usd"] = round(st["unrealized_pnl_usd"] + upnl, 2)
            open_list.append(
                {
                    "id": tid,
                    "signal_id": sig_id or "",
                    "signal_created_ms": int(sig_c_ms or 0),
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

        leaderboard: List[Dict[str, Any]] = []
        for s_id, st in by_strat.items():
            n = st["closed_trades"]
            pf = round(st["gross_profit"] / st["gross_loss"], 2) if st["gross_loss"] > 0 else None
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

    def get_strategy_detail(
        self,
        strategy_id: str,
        bbo_map: Optional[Dict[str, Tuple[float, float]]] = None,
    ) -> Dict[str, Any]:
        """Возвращает полную статистику Live Shadow для конкретной стратегии: KPI, кривую капитала, открытые и закрытые сделки."""
        sid = str(strategy_id or "").strip()
        bbo = bbo_map or {}
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, symbol, side, status, opened_ts, closed_ts, entry_px, sl_px, tp_px, exit_px, qty, notional_usd, risk_usd, fees_usd, pnl_usd, realized_r, exit_reason, signal_id, signal_created_ms "
                "FROM shadow_trades WHERE strategy_id=? ORDER BY opened_ts ASC",
                (sid,),
            ).fetchall()

        open_list: List[Dict[str, Any]] = []
        closed_list: List[Dict[str, Any]] = []
        curve: List[float] = [self.POD_CAPITAL_USD]
        exit_counts: Dict[str, int] = {"TP": 0, "SL": 0, "TIME_STOP": 0, "OTHER": 0}
        wins = losses = 0
        gp = gl = net_pnl = upnl_sum = r_sum = hold_h_sum = fees_sum = 0.0
        eq_cur = eq_peak = max_dd = 0.0

        open_rows = [r for r in rows if r[3] == "OPEN"]
        closed_rows = sorted(
            (r for r in rows if r[3] != "OPEN"),
            key=lambda r: (float(r[5] or 0.0), float(r[4] or 0.0)),
        )
        for r in open_rows + closed_rows:
            tid, sym, side, status, o_ts, c_ts, e_px, sl_px, tp_px, x_px, qty, notional, risk_usd, fees, pnl, r_mult, reason, sig_id, sig_c_ms = r
            if status == "OPEN":
                bid, ask = bbo.get(sym, (0.0, 0.0))
                mark = bid if (side == "Buy" and bid > 0) else (ask if (side == "Sell" and ask > 0) else e_px)
                upnl = round(((mark - e_px) * qty if side == "Buy" else (e_px - mark) * qty), 2)
                upnl_sum = round(upnl_sum + upnl, 2)
                open_list.append(
                    {
                        "id": tid,
                        "signal_id": sig_id or "",
                        "signal_created_ms": int(sig_c_ms or 0),
                        "symbol": sym,
                        "side": side,
                        "entry_px": round(e_px, 6),
                        "mark_px": round(mark, 6),
                        "sl_px": round(sl_px, 6),
                        "tp_px": round(tp_px, 6),
                        "qty": round(qty, 6),
                        "notional_usd": round(notional, 2),
                        "risk_usd": round(risk_usd, 2),
                        "upnl_usd": upnl,
                        "age_hours": round(max(0.0, (time.time() - o_ts) / 3600.0), 1),
                        "opened_at": datetime.datetime.fromtimestamp(o_ts, tz=datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
                    }
                )
            else:
                if pnl > 0:
                    wins += 1
                    gp += pnl
                elif pnl < 0:
                    losses += 1
                    gl += abs(pnl)
                net_pnl += pnl
                r_sum += r_mult
                fees_sum += float(fees or 0.0)
                hold_h = round(max(0.0, (c_ts - o_ts) / 3600.0), 2)
                hold_h_sum += hold_h
                eq_cur += pnl
                curve.append(round(self.POD_CAPITAL_USD + eq_cur, 2))
                r_key = str(reason or "").upper()
                if r_key in ("TP", "SL", "TIME_STOP"):
                    exit_counts[r_key] += 1
                else:
                    exit_counts["OTHER"] += 1
                if eq_cur > eq_peak:
                    eq_peak = eq_cur
                dd = eq_peak - eq_cur
                if dd > max_dd:
                    max_dd = dd
                closed_list.append(
                    {
                        "id": tid,
                        "signal_id": sig_id or "",
                        "signal_created_ms": int(sig_c_ms or 0),
                        "symbol": sym,
                        "side": side,
                        "entry_px": round(e_px, 6),
                        "exit_px": round(x_px, 6),
                        "sl_px": round(sl_px, 6),
                        "tp_px": round(tp_px, 6),
                        "notional_usd": round(notional, 2),
                        "fees_usd": round(float(fees or 0.0), 3),
                        "pnl_usd": round(pnl, 2),
                        "realized_r": round(r_mult, 2),
                        "hold_hours": hold_h,
                        "exit_reason": reason,
                        "opened_at": datetime.datetime.fromtimestamp(o_ts, tz=datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
                        "closed_at": datetime.datetime.fromtimestamp(c_ts, tz=datetime.timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
                    }
                )

        n = len(closed_list)
        if len(curve) == 1:
            curve.append(round(self.POD_CAPITAL_USD + upnl_sum, 2))
        return {
            "strategy_id": sid,
            "pod_capital_usd": self.POD_CAPITAL_USD,
            "pod_equity_usd": round(self.POD_CAPITAL_USD + net_pnl + upnl_sum, 2),
            "risk_per_trade_usd": self.RISK_PER_TRADE_USD,
            "open_trades": len(open_list),
            "closed_trades": n,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / n) * 100.0, 1) if n > 0 else 0.0,
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "net_pnl_usd": round(net_pnl, 2),
            "unrealized_pnl_usd": round(upnl_sum, 2),
            "total_fees_usd": round(fees_sum, 3),
            "avg_r": round(r_sum / n, 3) if n > 0 else None,
            "avg_hold_hours": round(hold_h_sum / n, 1) if n > 0 else None,
            "max_dd_usd": round(max_dd, 2),
            "exit_breakdown": exit_counts,
            "equity_curve": curve[-120:],
            "open_positions": open_list[::-1],
            "closed_trades_list": closed_list[-60:][::-1],
        }

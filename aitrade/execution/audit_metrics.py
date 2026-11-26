# -*- coding: utf-8 -*-
"""
Сквозная трассировка исполнения B1/T1, раздельный учёт боевых стратегий (без legacy)
и скользящая 24-часовая воронка стратегий (StrategyFunnel24h).
"""

from __future__ import annotations

import datetime
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from aitrade.storage import Ledger, atomic_json

TRACKED_STRATEGIES = (
    "T1_DONCHIAN_TSMOM",
    "B1_SQUEEZE_BREAKOUT",
    "B2_DONCHIAN_INTRADAY",
    "B4_ORB_SESSION_BREAKOUT",
    "B5_OI_TREND_EXPANSION",
    "B6_EMA_PULLBACK_TREND",
    "B7_FUNDING_SQUEEZE_MOMENTUM",
    "T2_RS_BTC_BREAKOUT",
    "T3_KELTNER_TREND_4H",
    "C2_MTF_COMBO_BREAKOUT",
    "C3_ENSEMBLE_VOTE_2OF3",
    "S1_TREND_RETEST",
    "S3_VWAP_RANGE",
    "AUTOTRADE_MOMENTUM_FUNDING",
)

LIVE_ACTIVE_STRATEGIES = (
    "T1_DONCHIAN_TSMOM",
    "B1_SQUEEZE_BREAKOUT",
    "B2_DONCHIAN_INTRADAY",
    "B4_ORB_SESSION_BREAKOUT",
    "B5_OI_TREND_EXPANSION",
    "B6_EMA_PULLBACK_TREND",
    "B7_FUNDING_SQUEEZE_MOMENTUM",
    "T2_RS_BTC_BREAKOUT",
    "T3_KELTNER_TREND_4H",
    "C2_MTF_COMBO_BREAKOUT",
    "C3_ENSEMBLE_VOTE_2OF3",
    "S1_TREND_RETEST",
)

STRATEGY_HORIZON_LABELS = {
    "T1_DONCHIAN_TSMOM": "Многодневный (4h, до 10д · WFA PASSED)",
    "B1_SQUEEZE_BREAKOUT": "Разгон (15m/1h, до 18ч · WFA PASSED)",
    "B2_DONCHIAN_INTRADAY": "Demo Lab $1k (1h Donchian, до 18ч)",
    "B3_VWAP_MOMENTUM_IMPULSE": "Архив (отключена после провала живого теста)",
    "B4_ORB_SESSION_BREAKOUT": "Demo Lab $1k (15m Session ORB, до 8ч)",
    "B5_OI_TREND_EXPANSION": "Demo Lab $1k (15m OI-пробой, до 12ч)",
    "B6_EMA_PULLBACK_TREND": "Demo Lab $1k (15m откат к EMA21, до 12ч)",
    "B7_FUNDING_SQUEEZE_MOMENTUM": "Demo Lab $1k (1h сквиз фандинга, до 18ч)",
    "T2_RS_BTC_BREAKOUT": "Demo Lab $1k (1h RS vs BTC, до 48ч)",
    "T3_KELTNER_TREND_4H": "Demo Lab $1k (4h Keltner тренд, до 7д)",
    "C2_MTF_COMBO_BREAKOUT": "Demo Lab $1k (Комбо 4h+RS+VWAP, до 36ч)",
    "C3_ENSEMBLE_VOTE_2OF3": "Demo Lab $1k (Ансамбль ≥2 голосов, до 24ч)",
    "S1_TREND_RETEST": "Demo Lab $1k (15m ретест тренда, до 18ч)",
}


def normalize_strategy_id(raw: Any) -> str:
    s = str(raw or "").strip()
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    return s or "UNATTRIBUTED"


def _fmt_utc_ms(ms: int) -> str:
    if not ms or ms <= 0:
        return "—"
    return datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class StrategyFunnel24h:
    """
    Хранит скользящую 24-часовую воронку по каждой стратегии:
    окна допуска -> кандидаты -> блок WFA/ML -> блок Guard/биржа -> отправлено -> исполнено
    + конкретная причина простоя (last_idle_reason).
    """

    WINDOW_SEC = 86400.0

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._events: List[Dict[str, Any]] = []
        self._last_idle_reason: Dict[str, str] = {}
        self._last_updated_ts: float = 0.0
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._events = list(raw.get("events") or [])
                self._last_idle_reason = dict(raw.get("last_idle_reason") or {})
                self._last_updated_ts = float(raw.get("updated_at_ts") or 0.0)
                self._prune(time.time())
        except Exception:
            pass

    def _save(self) -> None:
        if not self.path:
            return
        try:
            atomic_json(
                self.path,
                {
                    "updated_at_ts": self._last_updated_ts,
                    "last_idle_reason": self._last_idle_reason,
                    "events": self._events[-5000:],
                },
            )
        except Exception:
            pass

    def _prune(self, now_ts: float) -> None:
        cutoff = now_ts - self.WINDOW_SEC
        if self._events and float(self._events[0].get("ts", 0.0)) < cutoff:
            self._events = [e for e in self._events if float(e.get("ts", 0.0)) >= cutoff]

    def record_scan(self, strategy_diagnostics: Dict[str, Dict[str, Any]], now_ts: Optional[float] = None) -> None:
        if not isinstance(strategy_diagnostics, dict):
            return
        ts = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            self._prune(ts)
            for s_id, diag in strategy_diagnostics.items():
                if not isinstance(diag, dict):
                    continue
                norm_id = normalize_strategy_id(s_id)
                self._events.append(
                    {
                        "ts": ts,
                        "kind": "scan",
                        "strategy": norm_id,
                        "eligible": 1 if diag.get("eligible_window") else 0,
                        "candidates": int(diag.get("raw_candidates", 0) or 0),
                        "blocked_wfa_ml": int(diag.get("blocked_wfa_ml", 0) or 0),
                    }
                )
                reason = str(diag.get("idle_reason") or "").strip()
                if reason:
                    self._last_idle_reason[norm_id] = reason
            self._last_updated_ts = ts
            self._save()

    def record_guard_block(self, strategy_id: str, reason: str, now_ts: Optional[float] = None) -> None:
        ts = float(now_ts if now_ts is not None else time.time())
        norm_id = normalize_strategy_id(strategy_id)
        with self._lock:
            self._prune(ts)
            self._events.append(
                {
                    "ts": ts,
                    "kind": "guard_block",
                    "strategy": norm_id,
                    "reason": str(reason or "")[:140],
                }
            )
            if reason:
                self._last_idle_reason[norm_id] = f"Guard/Биржа: {str(reason)[:120]}"
            self._last_updated_ts = ts
            self._save()

    def record_submit(self, strategy_id: str, now_ts: Optional[float] = None) -> None:
        ts = float(now_ts if now_ts is not None else time.time())
        norm_id = normalize_strategy_id(strategy_id)
        with self._lock:
            self._prune(ts)
            self._events.append({"ts": ts, "kind": "submit", "strategy": norm_id})
            self._last_idle_reason[norm_id] = "Заявка отправлена на биржу"
            self._last_updated_ts = ts
            self._save()

    def get_summary(
        self,
        filled_by_strategy: Optional[Dict[str, int]] = None,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        ts = float(now_ts if now_ts is not None else time.time())
        filled_map = filled_by_strategy or {}
        with self._lock:
            self._prune(ts)
            stats: Dict[str, Dict[str, Any]] = {}
            all_ids = list(TRACKED_STRATEGIES) + sorted(
                {e.get("strategy", "") for e in self._events if e.get("strategy") and e.get("strategy") not in TRACKED_STRATEGIES}
            )
            for s_id in all_ids:
                stats[s_id] = {
                    "strategy_id": s_id,
                    "scans_total": 0,
                    "scans_eligible": 0,
                    "candidates": 0,
                    "blocked_wfa_ml": 0,
                    "blocked_guard_exchange": 0,
                    "submitted": 0,
                    "filled_24h": int(filled_map.get(s_id, 0) or 0),
                    "last_idle_reason": self._last_idle_reason.get(s_id, "Ожидание первого скана"),
                }
            for e in self._events:
                s_id = e.get("strategy", "")
                if s_id not in stats:
                    continue
                row = stats[s_id]
                k = e.get("kind")
                if k == "scan":
                    row["scans_total"] += 1
                    row["scans_eligible"] += int(e.get("eligible", 0) or 0)
                    row["candidates"] += int(e.get("candidates", 0) or 0)
                    row["blocked_wfa_ml"] += int(e.get("blocked_wfa_ml", 0) or 0)
                elif k == "guard_block":
                    row["blocked_guard_exchange"] += 1
                elif k == "submit":
                    row["submitted"] += 1

            return {
                "window_hours": 24,
                "updated_at": _fmt_utc_ms(int(self._last_updated_ts * 1000)) if self._last_updated_ts else "—",
                "strategies": stats,
            }


def _compute_max_drawdown(pnls_chronological: List[float]) -> float:
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls_chronological:
        eq += p
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def compute_live_strategy_metrics(
    ledgers: Iterable[Tuple[str, Ledger]],
    target_strategies: Tuple[str, ...] = LIVE_ACTIVE_STRATEGIES,
    now_ts: Optional[float] = None,
    trace_limit: Optional[int] = 50,
) -> Dict[str, Any]:
    """
    Строит раздельный учёт боевых стратегий (T1_DONCHIAN_TSMOM, B1_SQUEEZE_BREAKOUT)
    в отрыве от legacy-истории и формирует сквозную трассировку исполнения:
    intent_id -> signal_entry_px -> actual_fill_px -> slippage_bps -> fees -> hold_hours -> realized_r.
    """
    now_s = float(now_ts if now_ts is not None else time.time())
    cutoff_24h_ms = int((now_s - 86400.0) * 1000)

    target_set = set(target_strategies)
    closed_by_strat: Dict[str, List[Dict[str, Any]]] = {s: [] for s in target_strategies}
    filled_24h_by_strat: Dict[str, int] = {}
    traces: List[Dict[str, Any]] = []
    legacy_trades = 0
    legacy_net_usd = 0.0

    seen_closed = set()
    seen_intents = set()
    ledger_errors: List[str] = []

    for acc_name, ledger in ledgers:
        try:
            intents_map, active_lots, _ = ledger._execution_lifecycles()
            closed_rows = ledger.attribute(ledger.records("closed"))
            executions = ledger.records("executions")
            exit_reason_by_oid: Dict[str, str] = {}
            with ledger.connect() as conn:
                for (raw_ev,) in conn.execute("SELECT payload FROM events WHERE kind='EXIT_SUBMITTED'").fetchall():
                    try:
                        ev_p = json.loads(raw_ev)
                        e_oid = str(ev_p.get("orderId") or "")
                        e_rsn = str(ev_p.get("exit_reason") or "").strip()
                        if e_oid and e_rsn:
                            exit_reason_by_oid[e_oid] = e_rsn
                    except Exception:
                        continue
        except Exception as exc:
            ledger_errors.append(f"{acc_name}: {str(exc)[:140]}")
            continue

        # Индексируем фактические исполнения (execPrice, execQty, execFee, execTime) по orderLinkId / orderId
        exec_by_link: Dict[str, List[Dict[str, Any]]] = {}
        exec_by_oid: Dict[str, List[Dict[str, Any]]] = {}
        for ex in executions:
            if ex.get("execType", "Trade") != "Trade":
                continue
            lnk = str(ex.get("orderLinkId") or "")
            oid = str(ex.get("orderId") or "")
            if lnk:
                exec_by_link.setdefault(lnk, []).append(ex)
            if oid:
                exec_by_oid.setdefault(oid, []).append(ex)

        # Связываем закрытые сделки по entry_intent_id
        closed_by_intent: Dict[str, List[Dict[str, Any]]] = {}
        for r in closed_rows:
            oid = str(r.get("orderId") or r.get("updatedTime") or "")
            sym = str(r.get("symbol") or "")
            dk = (acc_name, oid, sym)
            if dk in seen_closed:
                continue
            seen_closed.add(dk)

            clean_s = normalize_strategy_id(r.get("strategy_id"))
            pnl = float(r.get("closedPnl", 0.0) or 0.0)
            iid = str(r.get("entry_intent_id") or "")
            if iid:
                closed_by_intent.setdefault(iid, []).append(r)

            if clean_s not in target_set:
                if abs(pnl) > 1e-9:
                    legacy_trades += 1
                    legacy_net_usd += pnl
            else:
                closed_by_strat[clean_s].append(r)

        # Собираем активные (открытые) лоты по intent_id
        open_intent_ids = set()
        for lots_list in active_lots.values():
            for lot in lots_list:
                if lot.get("intent_id") and float(lot.get("remaining", 0.0) or 0.0) > 1e-10:
                    open_intent_ids.add(str(lot["intent_id"]))

        # Сквозная трассировка по всем intents целевых стратегий (+ учёт filled_24h)
        for iid, intent_obj in intents_map.items():
            if (acc_name, iid) in seen_intents:
                continue
            seen_intents.add((acc_name, iid))

            clean_s = normalize_strategy_id(intent_obj.get("strategy_id"))
            side = str(intent_obj.get("side") or "")
            sym = str(intent_obj.get("symbol") or "")
            sig_ctx = intent_obj.get("signal_context") if isinstance(intent_obj.get("signal_context"), dict) else {}
            sig_id = str(intent_obj.get("signal_id") or sig_ctx.get("signal_id") or "").strip()
            sig_created_ms = int(intent_obj.get("signal_created_ms") or sig_ctx.get("signal_created_ms") or 0)
            sig_entry = float(intent_obj.get("entry") or 0.0)
            sl_px = float(intent_obj.get("sl") or 0.0)
            tp_px = float(intent_obj.get("tp") or 0.0)
            req_qty = float(intent_obj.get("requestedQty") or intent_obj.get("qty") or 0.0)
            created_ms = int(float(intent_obj.get("created") or 0.0) * 1000)

            # Находим входные executions по link или orderIds
            entry_execs: List[Dict[str, Any]] = []
            seen_eids = set()
            cand_execs = list(exec_by_link.get(iid, []))
            for oid in list(intent_obj.get("orderIds") or []) + ([intent_obj.get("orderId")] if intent_obj.get("orderId") else []):
                if oid:
                    cand_execs.extend(exec_by_oid.get(str(oid), []))
            for ex in cand_execs:
                eid = str(ex.get("execId") or "")
                if eid and eid in seen_eids:
                    continue
                if eid:
                    seen_eids.add(eid)
                if float(ex.get("closedSize") or 0.0) < float(ex.get("execQty") or 0.0):
                    entry_execs.append(ex)

            filled_qty = sum(float(e.get("execQty") or 0.0) for e in entry_execs)
            if filled_qty > 0:
                actual_fill_px = sum(float(e.get("execPrice") or 0.0) * float(e.get("execQty") or 0.0) for e in entry_execs) / filled_qty
                opened_ms = min(int(e.get("execTime") or created_ms) for e in entry_execs)
                entry_fees = sum(float(e.get("execFee") or 0.0) for e in entry_execs)
                if opened_ms >= cutoff_24h_ms:
                    filled_24h_by_strat[clean_s] = filled_24h_by_strat.get(clean_s, 0) + 1
            else:
                actual_fill_px = 0.0
                opened_ms = created_ms
                entry_fees = 0.0

            if clean_s not in target_set:
                continue

            # Если есть закрытия по этому intent, агрегируем PnL, выход, комиссии и время удержания
            c_rows = closed_by_intent.get(iid, [])
            closed_pnl_sum = sum(float(c.get("closedPnl") or 0.0) for c in c_rows)
            exit_fees = 0.0
            exit_reason = ""
            for c in c_rows:
                c_oid = str(c.get("orderId") or "")
                if not exit_reason and c_oid in exit_reason_by_oid:
                    exit_reason = exit_reason_by_oid[c_oid]
                for ex in exec_by_oid.get(c_oid, []):
                    exit_fees += float(ex.get("execFee") or 0.0)
                    if not exit_reason:
                        st_ex = str(ex.get("stopOrderType") or "").lower()
                        if "take" in st_ex or "profit" in st_ex:
                            exit_reason = "TP"
                        elif "stop" in st_ex:
                            exit_reason = "SL"
            if c_rows and not exit_reason:
                avg_exit_px = float(c_rows[-1].get("avgExitPrice") or 0.0)
                if avg_exit_px > 0 and tp_px > 0 and abs(avg_exit_px - tp_px) / tp_px <= 0.0025:
                    exit_reason = "TP"
                elif avg_exit_px > 0 and sl_px > 0 and abs(avg_exit_px - sl_px) / sl_px <= 0.0025:
                    exit_reason = "SL"
                else:
                    exit_reason = "EXCHANGE_CLOSE"
            total_fees = round(entry_fees + exit_fees, 4)

            if c_rows and actual_fill_px <= 0:
                actual_fill_px = float(c_rows[0].get("avgEntryPrice") or 0.0)
                filled_qty = sum(float(c.get("closedSize") or c.get("qty") or 0.0) for c in c_rows)

            slip_bps: Optional[float] = None
            if sig_entry > 0 and actual_fill_px > 0:
                if side == "Buy":
                    slip_bps = round(((actual_fill_px - sig_entry) / sig_entry) * 10000.0, 2)
                elif side == "Sell":
                    slip_bps = round(((sig_entry - actual_fill_px) / sig_entry) * 10000.0, 2)

            fill_latency_ms: Optional[int] = None
            if filled_qty > 0 and opened_ms > 0 and sig_created_ms > 0:
                fill_latency_ms = max(0, int(opened_ms - sig_created_ms))

            planned_risk_usd = abs(sig_entry - sl_px) * (filled_qty if filled_qty > 0 else req_qty) if (sig_entry > 0 and sl_px > 0) else 0.0
            realized_r = round(closed_pnl_sum / planned_risk_usd, 3) if (c_rows and planned_risk_usd > 1e-6) else None

            closed_ms = max((int(c.get("updatedTime") or 0) for c in c_rows), default=0)
            if c_rows and closed_ms > opened_ms > 0:
                hold_hours: Optional[float] = round((closed_ms - opened_ms) / 3_600_000.0, 2)
            elif iid in open_intent_ids and opened_ms > 0:
                hold_hours = round(max(0.0, (now_s * 1000.0 - opened_ms) / 3_600_000.0), 2)
            else:
                hold_hours = None

            if c_rows and iid not in open_intent_ids:
                status = "CLOSED"
            elif iid in open_intent_ids:
                status = "OPEN"
            elif filled_qty > 0:
                status = "FILLED"
            else:
                status = "REJECTED" if intent_obj.get("error") else "PENDING"

            traces.append(
                {
                    "intent_id": iid,
                    "signal_id": sig_id,
                    "signal_created_ms": sig_created_ms,
                    "fill_latency_ms": fill_latency_ms,
                    "exit_reason": exit_reason,
                    "account": acc_name,
                    "strategy_id": clean_s,
                    "symbol": sym or (c_rows[0].get("symbol") if c_rows else ""),
                    "side": side,
                    "status": status,
                    "signal_entry_px": round(sig_entry, 6) if sig_entry > 0 else None,
                    "actual_fill_px": round(actual_fill_px, 6) if actual_fill_px > 0 else None,
                    "slippage_bps": slip_bps,
                    "fees_usd": total_fees,
                    "planned_risk_usd": round(planned_risk_usd, 2) if planned_risk_usd > 0 else None,
                    "closed_pnl_usd": round(closed_pnl_sum, 2) if c_rows else None,
                    "realized_r": realized_r,
                    "hold_hours": hold_hours,
                    "opened_ms": opened_ms,
                    "opened_at_utc": _fmt_utc_ms(opened_ms),
                    "closed_at_utc": _fmt_utc_ms(closed_ms),
                }
            )

    traces.sort(key=lambda t: int(t.get("opened_ms") or 0), reverse=True)

    # Агрегируем раздельные метрики по каждой целевой стратегии
    by_strategy: Dict[str, Dict[str, Any]] = {}
    for s_id in target_strategies:
        c_rows = sorted(closed_by_strat.get(s_id, []), key=lambda r: int(r.get("updatedTime") or 0))
        pnls = [float(r.get("closedPnl") or 0.0) for r in c_rows if abs(float(r.get("closedPnl") or 0.0)) > 1e-9]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        pf = round(gp / gl, 2) if gl > 0 else None
        wr = round((len(wins) / len(pnls)) * 100.0, 1) if pnls else 0.0

        s_traces = [t for t in traces if t["strategy_id"] == s_id]
        closed_traces = [t for t in s_traces if t["status"] == "CLOSED"]
        open_traces = [t for t in s_traces if t["status"] == "OPEN"]
        holds = [t["hold_hours"] for t in closed_traces if t["hold_hours"] is not None]
        rs = [t["realized_r"] for t in closed_traces if t["realized_r"] is not None]
        slips = [t["slippage_bps"] for t in s_traces if t["slippage_bps"] is not None]
        fees_sum = round(sum(float(t.get("fees_usd") or 0.0) for t in s_traces), 4)

        by_strategy[s_id] = {
            "strategy_id": s_id,
            "horizon_label": STRATEGY_HORIZON_LABELS.get(s_id, "Интрадей / Разгон"),
            "trades": len(pnls),
            "open_trades": len(open_traces),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": wr,
            "profit_factor": pf,
            "net_pnl_usd": round(sum(pnls), 2),
            "max_drawdown_usd": _compute_max_drawdown(pnls),
            "avg_hold_hours": round(sum(holds) / len(holds), 2) if holds else None,
            "avg_realized_r": round(sum(rs) / len(rs), 3) if rs else None,
            "avg_slippage_bps": round(sum(slips) / len(slips), 2) if slips else None,
            "total_fees_usd": fees_sum,
            "traced_intents": len(s_traces),
        }

    return {
        "strategies": by_strategy,
        "execution_traces": traces if trace_limit is None else traces[: max(0, int(trace_limit))],
        "filled_24h_by_strategy": filled_24h_by_strat,
        "ledger_errors": ledger_errors,
        "legacy_isolated": {
            "trades": legacy_trades,
            "net_pnl_usd": round(legacy_net_usd, 2),
        },
    }


def build_shadow_demo_reconciliation(
    shadow_engine: Any,
    ledgers: Iterable[Tuple[str, Ledger]],
    strategy_id: Optional[str] = None,
    limit: int = 200,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Сквозная сверка Shadow vs Bybit Demo по общему signal_id:
    - Фильтрация по стратегии и периоду выполняется ДО постраничного лимита (итоги и причины не зависят от limit).
    - Исполнения Demo берутся по полному журналу (trace_limit=None), без обрезки до 50 трасс.
    - Ошибки чтения любого источника отражаются явно (degraded=True, ok=False, SOURCE_UNAVAILABLE).
    """
    target_strat = normalize_strategy_id(strategy_id) if strategy_id else ""
    page_limit = max(1, int(limit))
    since_val = int(since_ms) if since_ms is not None and int(since_ms) > 0 else None
    until_val = int(until_ms) if until_ms is not None and int(until_ms) > 0 else None

    def _in_period(ts_ms: int) -> bool:
        if since_val is not None and ts_ms < since_val:
            return False
        if until_val is not None and ts_ms > until_val:
            return False
        return True

    shadow_trades_by_sig: Dict[str, Dict[str, Any]] = {}
    shadow_decisions_by_sig: Dict[str, Dict[str, Any]] = {}
    shadow_source: Dict[str, Any] = {
        "ok": True,
        "available": bool(shadow_engine is not None and hasattr(shadow_engine, "_connect")),
        "complete": True,
        "error": "",
    }
    if not shadow_source["available"]:
        shadow_source["ok"] = False
        shadow_source["complete"] = False
        shadow_source["error"] = "Источник Shadow не подключён"
    else:
        try:
            with shadow_engine._lock, shadow_engine._connect() as conn:
                if target_strat:
                    t_rows = conn.execute(
                        "SELECT id, signal_id, signal_created_ms, strategy_id, symbol, side, status, opened_ts, closed_ts, "
                        "entry_px, exit_px, sl_px, tp_px, fees_usd, pnl_usd, realized_r, exit_reason "
                        "FROM shadow_trades WHERE signal_id != '' AND (strategy_id = ? OR strategy_id LIKE ?) "
                        "ORDER BY opened_ts DESC",
                        (target_strat, f"%:{target_strat}"),
                    ).fetchall()
                else:
                    t_rows = conn.execute(
                        "SELECT id, signal_id, signal_created_ms, strategy_id, symbol, side, status, opened_ts, closed_ts, "
                        "entry_px, exit_px, sl_px, tp_px, fees_usd, pnl_usd, realized_r, exit_reason "
                        "FROM shadow_trades WHERE signal_id != '' ORDER BY opened_ts DESC"
                    ).fetchall()
                for r in t_rows:
                    sid_norm = normalize_strategy_id(r[3])
                    if target_strat and sid_norm != target_strat:
                        continue
                    sig_id = str(r[1] or "").strip()
                    if sig_id and sig_id not in shadow_trades_by_sig:
                        shadow_trades_by_sig[sig_id] = {
                            "trade_id": r[0],
                            "signal_id": sig_id,
                            "signal_created_ms": int(r[2] or 0),
                            "strategy_id": sid_norm,
                            "symbol": r[4],
                            "side": r[5],
                            "status": r[6],
                            "opened_ms": int(float(r[7] or 0.0) * 1000),
                            "closed_ms": int(float(r[8] or 0.0) * 1000) if r[8] else 0,
                            "entry_px": round(float(r[9] or 0.0), 6),
                            "exit_px": round(float(r[10] or 0.0), 6) if r[10] else None,
                            "sl_px": round(float(r[11] or 0.0), 6),
                            "tp_px": round(float(r[12] or 0.0), 6),
                            "fees_usd": round(float(r[13] or 0.0), 4),
                            "pnl_usd": round(float(r[14] or 0.0), 2) if r[6] == "CLOSED" else None,
                            "realized_r": round(float(r[15] or 0.0), 3) if r[6] == "CLOSED" else None,
                            "exit_reason": r[16] or "",
                        }
                if target_strat:
                    d_rows = conn.execute(
                        "SELECT ts, signal_id, strategy_id, symbol, side, decision, reason, shadow_trade_id "
                        "FROM shadow_signal_decisions WHERE signal_id != '' AND (strategy_id = ? OR strategy_id LIKE ?) "
                        "ORDER BY ts DESC",
                        (target_strat, f"%:{target_strat}"),
                    ).fetchall()
                else:
                    d_rows = conn.execute(
                        "SELECT ts, signal_id, strategy_id, symbol, side, decision, reason, shadow_trade_id "
                        "FROM shadow_signal_decisions WHERE signal_id != '' ORDER BY ts DESC"
                    ).fetchall()
                for r in d_rows:
                    sid_norm = normalize_strategy_id(r[2])
                    if target_strat and sid_norm != target_strat:
                        continue
                    sig_id = str(r[1] or "").strip()
                    if sig_id and sig_id not in shadow_decisions_by_sig:
                        shadow_decisions_by_sig[sig_id] = {
                            "ts": float(r[0] or 0.0),
                            "signal_id": sig_id,
                            "strategy_id": sid_norm,
                            "symbol": r[3],
                            "side": r[4],
                            "decision": r[5],
                            "reason": r[6] or "",
                            "trade_id": r[7] or "",
                        }
        except Exception as exc:
            shadow_source["ok"] = False
            shadow_source["complete"] = False
            shadow_source["error"] = str(exc)[:200]

    ledger_list = list(ledgers)
    demo_source: Dict[str, Any] = {
        "ok": True,
        "available": len(ledger_list) > 0,
        "complete": True,
        "error": "",
        "ledgers_checked": len(ledger_list),
        "ledgers_failed": 0,
    }
    demo_errors: List[str] = []
    if not ledger_list:
        demo_source["ok"] = False
        demo_source["complete"] = False
        demo_source["error"] = "Журналы Demo не подключены"

    live_metrics = compute_live_strategy_metrics(ledger_list, trace_limit=None)
    if live_metrics.get("ledger_errors"):
        demo_errors.extend(live_metrics["ledger_errors"])

    demo_traces_by_sig: Dict[str, Dict[str, Any]] = {}
    for tr in live_metrics.get("execution_traces") or []:
        sig_id = str(tr.get("signal_id") or "").strip()
        if not sig_id:
            continue
        if target_strat and normalize_strategy_id(tr.get("strategy_id")) != target_strat:
            continue
        if sig_id not in demo_traces_by_sig:
            demo_traces_by_sig[sig_id] = tr

    demo_decisions_by_sig: Dict[str, Dict[str, Any]] = {}
    for acc_name, ledger in ledger_list:
        try:
            with ledger.connect() as conn:
                ev_rows = conn.execute(
                    "SELECT ts, payload FROM events WHERE kind='SIGNAL_DECISION' ORDER BY ts DESC"
                ).fetchall()
            for ts_v, raw_p in ev_rows:
                try:
                    p = json.loads(raw_p)
                except Exception:
                    continue
                sig_id = str(p.get("signal_id") or "").strip()
                if not sig_id:
                    continue
                sid_norm = normalize_strategy_id(p.get("strategy_id"))
                if target_strat and sid_norm != target_strat:
                    continue
                if sig_id not in demo_decisions_by_sig:
                    demo_decisions_by_sig[sig_id] = {
                        "ts": float(ts_v or 0.0),
                        "account": acc_name,
                        "signal_id": sig_id,
                        "signal_created_ms": int(p.get("signal_created_ms") or 0),
                        "strategy_id": sid_norm,
                        "symbol": str(p.get("symbol") or ""),
                        "side": str(p.get("side") or ""),
                        "decision": str(p.get("decision") or ""),
                        "reason": str(p.get("reason") or ""),
                        "intent_id": str(p.get("intent_id") or ""),
                    }
        except Exception as exc:
            demo_source["ledgers_failed"] += 1
            demo_errors.append(f"{acc_name}: {str(exc)[:140]}")

    if demo_errors:
        demo_source["ok"] = False
        demo_source["complete"] = False
        demo_source["error"] = "; ".join(dict.fromkeys(demo_errors))[:220]

    degraded = (not shadow_source["ok"]) or (not demo_source["ok"])
    all_sig_ids = set(shadow_trades_by_sig) | set(shadow_decisions_by_sig) | set(demo_traces_by_sig) | set(demo_decisions_by_sig)
    records: List[Dict[str, Any]] = []
    matched_both = 0
    matched_closed = 0
    shadow_only = 0
    demo_only = 0
    skipped_both = 0
    unverified = 0

    for sig_id in all_sig_ids:
        sh_t = shadow_trades_by_sig.get(sig_id)
        sh_d = shadow_decisions_by_sig.get(sig_id)
        dm_t = demo_traces_by_sig.get(sig_id)
        dm_d = demo_decisions_by_sig.get(sig_id)

        meta = sh_t or dm_t or dm_d or sh_d or {}
        created_ms = int(
            (sh_t or {}).get("signal_created_ms")
            or (dm_t or {}).get("signal_created_ms")
            or (dm_d or {}).get("signal_created_ms")
            or (sh_t or {}).get("opened_ms")
            or (dm_t or {}).get("opened_ms")
            or int(float((dm_d or sh_d or {}).get("ts") or 0.0) * 1000)
        )
        if not _in_period(created_ms):
            continue

        sh_entered = sh_t is not None
        dm_entered = dm_t is not None and dm_t.get("status") in ("OPEN", "CLOSED", "FILLED")

        if sh_entered and dm_entered:
            pair_status = "MATCHED_BOTH"
            matched_both += 1
            if sh_t.get("status") == "CLOSED" and dm_t.get("status") == "CLOSED":
                matched_closed += 1
        elif sh_entered:
            if not demo_source["ok"]:
                pair_status = "UNVERIFIED_DEMO_ERROR"
                unverified += 1
            else:
                pair_status = "SHADOW_ONLY"
                shadow_only += 1
        elif dm_entered:
            if not shadow_source["ok"]:
                pair_status = "UNVERIFIED_SHADOW_ERROR"
                unverified += 1
            else:
                pair_status = "DEMO_ONLY"
                demo_only += 1
        else:
            if degraded and ((not shadow_source["ok"] and sh_d is None) or (not demo_source["ok"] and dm_d is None)):
                pair_status = "SOURCE_UNAVAILABLE"
                unverified += 1
            else:
                pair_status = "SKIPPED_BOTH"
                skipped_both += 1

        if sh_entered:
            sh_dec = "ENTERED"
            sh_skip = ""
        elif sh_d is not None:
            sh_dec = sh_d.get("decision") or "SKIPPED"
            sh_skip = sh_d.get("reason", "")
        elif not shadow_source["ok"]:
            sh_dec = "SOURCE_UNAVAILABLE"
            sh_skip = "Данные Shadow недоступны"
        else:
            sh_dec = "NO_RECORD"
            sh_skip = ""

        if dm_entered:
            dm_dec = dm_d.get("decision") if dm_d else "SUBMITTED"
            dm_skip = ""
        elif dm_d is not None:
            dm_dec = dm_d.get("decision") or "SKIPPED"
            dm_skip = dm_d.get("reason", "")
        elif dm_t is not None:
            dm_dec = "SUBMITTED"
            dm_skip = ""
        elif not demo_source["ok"]:
            dm_dec = "SOURCE_UNAVAILABLE"
            dm_skip = "Данные Demo недоступны"
        else:
            dm_dec = "NO_RECORD"
            dm_skip = ""

        records.append(
            {
                "signal_id": sig_id,
                "signal_created_ms": created_ms,
                "signal_created_utc": _fmt_utc_ms(created_ms),
                "strategy_id": meta.get("strategy_id", ""),
                "symbol": meta.get("symbol", ""),
                "side": meta.get("side", ""),
                "pair_status": pair_status,
                "shadow": {
                    "decision": sh_dec,
                    "skip_reason": sh_skip,
                    "trade": sh_t,
                },
                "demo": {
                    "decision": dm_dec,
                    "skip_reason": dm_skip,
                    "fill_latency_ms": dm_t.get("fill_latency_ms") if dm_t else None,
                    "exit_reason": dm_t.get("exit_reason", "") if dm_t else "",
                    "trace": dm_t,
                },
            }
        )

    records.sort(key=lambda x: int(x.get("signal_created_ms") or 0), reverse=True)
    min_ms = min((int(r["signal_created_ms"]) for r in records if r.get("signal_created_ms")), default=None)
    max_ms = max((int(r["signal_created_ms"]) for r in records if r.get("signal_created_ms")), default=None)
    page = records[:page_limit]
    return {
        "ok": not degraded,
        "degraded": degraded,
        "strategy_filter": target_strat or None,
        "period": {
            "since_ms": since_val,
            "until_ms": until_val,
            "observed_min_ms": min_ms,
            "observed_max_ms": max_ms,
        },
        "sources": {
            "shadow": shadow_source,
            "demo": demo_source,
        },
        "summary": {
            "total_signals": len(records),
            "returned_signals": len(page),
            "page_limit": page_limit,
            "matched_both": matched_both,
            "matched_closed": matched_closed,
            "shadow_only": shadow_only,
            "demo_only": demo_only,
            "skipped_both": skipped_both,
            "unverified": unverified,
        },
        "signals": page,
    }

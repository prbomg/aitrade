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
    "B3_VWAP_MOMENTUM_IMPULSE",
    "B4_ORB_SESSION_BREAKOUT",
    "B5_OI_TREND_EXPANSION",
    "T2_RS_BTC_BREAKOUT",
    "C2_MTF_COMBO_BREAKOUT",
    "S1_TREND_RETEST",
    "S3_VWAP_RANGE",
    "AUTOTRADE_MOMENTUM_FUNDING",
)

LIVE_ACTIVE_STRATEGIES = (
    "T1_DONCHIAN_TSMOM",
    "B1_SQUEEZE_BREAKOUT",
    "B2_DONCHIAN_INTRADAY",
    "B3_VWAP_MOMENTUM_IMPULSE",
    "B4_ORB_SESSION_BREAKOUT",
    "B5_OI_TREND_EXPANSION",
    "T2_RS_BTC_BREAKOUT",
    "C2_MTF_COMBO_BREAKOUT",
    "S1_TREND_RETEST",
)

STRATEGY_HORIZON_LABELS = {
    "T1_DONCHIAN_TSMOM": "Многодневный (4h, до 10д · WFA PASSED)",
    "B1_SQUEEZE_BREAKOUT": "Разгон (15m/1h, до 18ч · WFA PASSED)",
    "B2_DONCHIAN_INTRADAY": "Demo Lab $1k (1h Donchian, до 18ч)",
    "B3_VWAP_MOMENTUM_IMPULSE": "Demo Lab $1k (15m VWAP импульс, до 8ч)",
    "B4_ORB_SESSION_BREAKOUT": "Demo Lab $1k (15m Session ORB, до 8ч)",
    "B5_OI_TREND_EXPANSION": "Demo Lab $1k (15m OI-пробой, до 12ч)",
    "T2_RS_BTC_BREAKOUT": "Demo Lab $1k (1h RS vs BTC, до 48ч)",
    "C2_MTF_COMBO_BREAKOUT": "Demo Lab $1k (Комбо 4h+RS+VWAP, до 36ч)",
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

    for acc_name, ledger in ledgers:
        try:
            intents_map, active_lots, _ = ledger._execution_lifecycles()
            closed_rows = ledger.attribute(ledger.records("closed"))
            executions = ledger.records("executions")
        except Exception:
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
            sig_entry = float(intent_obj.get("entry") or 0.0)
            sl_px = float(intent_obj.get("sl") or 0.0)
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
            for c in c_rows:
                c_oid = str(c.get("orderId") or "")
                for ex in exec_by_oid.get(c_oid, []):
                    exit_fees += float(ex.get("execFee") or 0.0)
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
        pf = round(gp / gl, 2) if gl > 0 else (None if gp == 0 else 99.0)
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
        "execution_traces": traces[:50],
        "filled_24h_by_strategy": filled_24h_by_strat,
        "legacy_isolated": {
            "trades": legacy_trades,
            "net_pnl_usd": round(legacy_net_usd, 2),
        },
    }

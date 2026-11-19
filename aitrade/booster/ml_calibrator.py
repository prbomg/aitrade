# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aitrade.storage import atomic_json


class AdaptiveMLCalibrator:
    """
    Калибратор весов стратегий (AItrade 2026):
    - Никакого вечного QUARANTINE по 3 стопам подряд.
    - Активация снижения весов только при trades >= 20 (COOLING 0.50x при PF < 0.90 или streak <= -5).
    - QUARANTINE (0.0x, блок входов) только при trades >= 25 и PF < 0.75.
    - Автовыход из QUARANTINE в COOLING (0.50x) через 48ч ИЛИ при PF >= 1.10 на последних 10 теневых сделках.
    - Запись на диск только при фактическом появлении новых закрытых сделок (без паразитных fsync каждые 5с).
    """

    DEFAULT_STRATEGIES = [
        "B1_SQUEEZE_BREAKOUT",
        "S1_TREND_RETEST",
        "S3_VWAP_RANGE",
        "AUTOTRADE_MOMENTUM_FUNDING",
        "SHORT SQUEEZE BREAKOUT",
        "LIQ CASCADE REVERSAL",
        "LONG SQUEEZE / DUMP",
    ]
    SESSIONS = ("ASIA", "EUROPE", "US_OVERLAP")
    QUARANTINE_TTL_SEC = 48 * 3600

    def __init__(self, data_dir: Path) -> None:
        self.file_path = data_dir / "ml_calibration.json"
        self.state = self._load()
        self._last_closed_sig: Optional[Tuple[int, int, float]] = None

    @staticmethod
    def detect_session_utc(timestamp_ms: Optional[int] = None) -> str:
        if timestamp_ms and int(timestamp_ms) > 0:
            dt = datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc)
        else:
            dt = datetime.now(tz=timezone.utc)
        h = dt.hour
        if 8 <= h < 13:
            return "EUROPE"
        if 13 <= h < 21:
            return "US_OVERLAP"
        return "ASIA"

    def _default_state(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "base_ml_threshold_pct": 58.0,
            "recommended_ml_threshold_pct": 58.0,
            "active_session_utc": self.detect_session_utc(),
            "symbol_strategy_map": {},
            "processed_trades": {},
            "quarantine_timers": {},
            "shadow_trades": {},
            "last_calibrated_at": "—",
            "portfolio_stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 1.0,
                "net_pnl_usd": 0.0,
            },
            "session_stats": {},
            "strategies": {},
        }

    def _load(self) -> Dict[str, Any]:
        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                d = self._default_state()
                d.update(data)
                return d
            except Exception:
                pass
        d = self._default_state()
        self._save(d)
        return d

    def _save(self, st: Dict[str, Any]) -> None:
        atomic_json(self.file_path, st)

    def register_entry(self, symbol: str, strategy_name: str) -> None:
        if not symbol or not strategy_name:
            return
        clean_name = str(strategy_name).strip()
        self.state.setdefault("symbol_strategy_map", {})[symbol] = clean_name
        self._save(self.state)

    def record_shadow_trade(self, strategy_name: str, pnl_usd: float) -> None:
        """Фиксирует виртуальную (теневую) сделку для автовыхода стратегии из QUARANTINE."""
        clean = strategy_name.split(":", 1)[1].strip() if ":" in strategy_name else strategy_name.strip()
        shadows = self.state.setdefault("shadow_trades", {}).setdefault(clean, [])
        shadows.append(float(pnl_usd))
        self.state["shadow_trades"][clean] = shadows[-20:]
        self._save(self.state)

    def reset_calibration(self, base_threshold_pct: float = 58.0) -> Dict[str, Any]:
        sym_map = self.state.get("symbol_strategy_map", {})
        self.state = self._default_state()
        self.state["base_ml_threshold_pct"] = float(base_threshold_pct or 58.0)
        self.state["recommended_ml_threshold_pct"] = float(base_threshold_pct or 58.0)
        self.state["symbol_strategy_map"] = sym_map
        self._last_closed_sig = None
        self._save(self.state)
        return self.state

    def set_enabled(self, enabled: bool, base_threshold_pct: float = 58.0) -> None:
        self.state["enabled"] = bool(enabled)
        self.state["base_ml_threshold_pct"] = float(base_threshold_pct or 58.0)
        self._save(self.state)

    def _infer_strategy_for_symbol(self, symbol: str, index_hint: int = 0) -> str:
        return "UNATTRIBUTED"

    @staticmethod
    def _calc_bucket_metrics(pnls: List[float]) -> Dict[str, Any]:
        cnt = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        wr = round((wins / cnt) * 100.0, 1) if cnt > 0 else 0.0
        gp = sum(p for p in pnls if p > 0)
        gl = sum(abs(p) for p in pnls if p < 0)
        pf = round(gp / max(0.01, gl), 2) if gl > 0 else None
        avg_win = (gp / wins) if wins > 0 else 0.0
        avg_loss = (gl / losses) if losses > 0 else 0.0
        payoff = round(avg_win / max(0.01, avg_loss), 2) if avg_loss > 0 else (2.0 if avg_win > 0 else 1.0)
        return {
            "trades": cnt,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": wr,
            "profit_factor": pf,
            "payoff_ratio": payoff,
            "net_pnl_usd": round(gp - gl, 2),
        }

    def _shadow_recovered(self, strategy_name: str) -> bool:
        shadows = (self.state.get("shadow_trades") or {}).get(strategy_name) or []
        if len(shadows) < 10:
            return False
        m = self._calc_bucket_metrics(shadows[-10:])
        return m["profit_factor"] is None or m["profit_factor"] >= 1.10

    def calibrate_from_closed_pnl(
        self,
        closed_pnl_list: List[Dict[str, Any]],
        force: bool = False,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        rows = closed_pnl_list or []
        last_upd = max((int(r.get("updatedTime", 0) or 0) for r in rows), default=0)
        pnl_sum = round(sum(float(r.get("closedPnl", 0) or 0) for r in rows), 4)
        sig = (len(rows), last_upd, pnl_sum)

        # Не перезаписываем JSON на диск, если закрытые сделки не менялись и не истёк таймер карантина
        q_timers = self.state.setdefault("quarantine_timers", {})
        any_expired = any(float(ts or 0) > 0 and now >= float(ts) for ts in q_timers.values())
        if not force and self._last_closed_sig == sig and not any_expired:
            return self.get_summary()
        self._last_closed_sig = sig

        strat_buckets: Dict[str, List[float]] = {name: [] for name in self.DEFAULT_STRATEGIES}
        strat_session_buckets: Dict[str, Dict[str, List[float]]] = {
            name: {s: [] for s in self.SESSIONS} for name in self.DEFAULT_STRATEGIES
        }
        global_session_buckets: Dict[str, List[float]] = {s: [] for s in self.SESSIONS}

        ordered = sorted(rows, key=lambda r: int(r.get("updatedTime", 0) or 0))
        total_wins = 0
        total_losses = 0
        gross_profit_all = 0.0
        gross_loss_all = 0.0

        for item in ordered:
            pnl = float(item.get("closedPnl", 0) or 0)
            if abs(pnl) < 1e-9:
                continue
            raw_s = str(item.get("strategy_id") or "UNATTRIBUTED")
            s_name = raw_s.split(":", 1)[1].strip() if ":" in raw_s else raw_s.strip()
            ts_ms = int(item.get("entry_created_ms") or item.get("createdTime") or item.get("updatedTime") or 0)
            sess = self.detect_session_utc(ts_ms)

            strat_buckets.setdefault(s_name, []).append(pnl)
            strat_session_buckets.setdefault(s_name, {s: [] for s in self.SESSIONS}).setdefault(sess, []).append(pnl)
            global_session_buckets.setdefault(sess, []).append(pnl)

            if pnl > 0:
                total_wins += 1
                gross_profit_all += pnl
            else:
                total_losses += 1
                gross_loss_all += abs(pnl)

        total_cnt = total_wins + total_losses
        port_wr = round((total_wins / total_cnt) * 100.0, 1) if total_cnt > 0 else 0.0
        port_pf = round(gross_profit_all / max(0.01, gross_loss_all), 2) if gross_loss_all > 0 else None
        net_pnl = round(gross_profit_all - gross_loss_all, 2)

        self.state["active_session_utc"] = self.detect_session_utc()
        self.state["portfolio_stats"] = {
            "total_trades": total_cnt,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate_pct": port_wr,
            "profit_factor": port_pf,
            "net_pnl_usd": net_pnl,
        }
        self.state["session_stats"] = {
            s: self._calc_bucket_metrics(pnls) for s, pnls in global_session_buckets.items()
        }

        base_thr = float(self.state.get("base_ml_threshold_pct", 58.0) or 58.0)
        rec_thr = base_thr
        if total_cnt >= 50 and port_pf is not None:
            if port_pf >= 1.45 and port_wr >= 56.0:
                rec_thr = max(54.0, round(base_thr - 2.0, 1))
            elif port_pf >= 1.20:
                rec_thr = max(55.0, round(base_thr - 1.0, 1))
            elif port_pf < 0.85 or port_wr < 42.0:
                rec_thr = min(66.0, round(base_thr + 4.0, 1))
            elif port_pf < 1.0:
                rec_thr = min(64.0, round(base_thr + 2.0, 1))

        self.state["recommended_ml_threshold_pct"] = rec_thr

        strategies_out: Dict[str, Any] = {}
        for s_name, pnls in strat_buckets.items():
            # Скользящее окно последних 30 сделок для расчёта актуального статуса
            recent_pnls = pnls[-30:]
            m = self._calc_bucket_metrics(recent_pnls)
            cnt = m["trades"]
            wins = m["wins"]
            losses = m["losses"]
            wr = m["win_rate_pct"]
            pf = m["profit_factor"]
            payoff = m["payoff_ratio"]
            net_s = m["net_pnl_usd"]

            streak = 0
            for p in reversed(recent_pnls):
                if p > 0:
                    if streak >= 0:
                        streak += 1
                    else:
                        break
                elif p < 0:
                    if streak <= 0:
                        streak -= 1
                    else:
                        break

            weight = 1.0
            status = "NORMAL"
            badge = "⚖️ НОРМА (1.0x)"
            prob_bonus_pct = 0.0
            q_until = float(q_timers.get(s_name, 0.0) or 0.0)

            if s_name != "UNATTRIBUTED" and pf is not None:
                if cnt >= 25 and pf < 0.75:
                    # Проверка автовыхода из QUARANTINE: истёк таймер 48ч ИЛИ теневые сделки дали PF >= 1.10
                    if (q_until > 0 and now >= q_until) or self._shadow_recovered(s_name):
                        weight = 0.50
                        status = "COOLING"
                        badge = "⚠️ ВЫХОД ИЗ КАРАНТИНА (0.50x)"
                        prob_bonus_pct = -3.0
                        q_timers[s_name] = 0.0
                    else:
                        if q_until <= 0:
                            q_until = now + self.QUARANTINE_TTL_SEC
                            q_timers[s_name] = q_until
                        weight = 0.0
                        status = "QUARANTINE"
                        badge = "🥶 КАРАНТИН (0.0x / 48ч)"
                        prob_bonus_pct = -6.0
                elif cnt >= 20 and (pf < 0.90 or streak <= -5):
                    weight = 0.50
                    status = "COOLING"
                    badge = "⚠️ ОХЛАЖДЕНИЕ (0.50x)"
                    prob_bonus_pct = -3.0
                    q_timers[s_name] = 0.0
                elif cnt >= 20 and pf >= 1.50 and wr >= 55.0 and payoff >= 0.85:
                    weight = 1.30
                    status = "BOOST"
                    badge = "🚀 БУСТ ЛИДЕРА (1.30x)"
                    prob_bonus_pct = 3.5
                    q_timers[s_name] = 0.0
                elif cnt >= 20 and (pf >= 1.20 or (streak >= 2 and pf >= 1.05)) and payoff >= 0.85:
                    weight = 1.15
                    status = "BOOST"
                    badge = "🟢 ПОВЫШЕН (1.15x)"
                    prob_bonus_pct = 2.0
                    q_timers[s_name] = 0.0

            sess_map: Dict[str, Any] = {}
            for s_key, s_pnls in strat_session_buckets.get(s_name, {}).items():
                sm = self._calc_bucket_metrics(s_pnls[-30:])
                s_mult = 1.0
                if sm["trades"] >= 8 and s_name != "UNATTRIBUTED" and sm["profit_factor"] is not None:
                    if sm["profit_factor"] < 0.85 or sm["payoff_ratio"] < 0.70:
                        s_mult = 0.65
                    elif sm["profit_factor"] >= 1.35 and sm["payoff_ratio"] >= 0.90:
                        s_mult = 1.15
                sm["session_multiplier"] = s_mult
                sess_map[s_key] = sm

            strategies_out[s_name] = {
                "strategy_name": s_name,
                "trades": cnt,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": wr,
                "profit_factor": pf,
                "payoff_ratio": payoff,
                "net_pnl_usd": net_s,
                "streak": streak,
                "streak_str": ("+" + str(streak) + "W") if streak > 0 else ((str(streak) + "L") if streak < 0 else "0"),
                "weight_multiplier": weight,
                "prob_adjustment_pct": prob_bonus_pct,
                "status": status,
                "badge": badge,
                "quarantine_until_ts": q_timers.get(s_name, 0.0),
                "sessions": sess_map,
            }

        self.state["strategies"] = strategies_out
        self.state["last_calibrated_at"] = datetime.now().strftime("%H:%M:%S")
        self._save(self.state)
        return self.get_summary()

    def get_strategy_modifier(self, strategy_name: str, session_utc: Optional[str] = None) -> Dict[str, Any]:
        if not self.state.get("enabled", True):
            return {"weight": 1.0, "prob_adj": 0.0, "status": "NORMAL", "badge": "⚖️ 1.0x", "session": "ALL"}
        strats = self.state.get("strategies", {})
        cur_sess = session_utc or self.detect_session_utc()
        raw_q = str(strategy_name)
        clean_q = raw_q.split(":", 1)[1].strip() if ":" in raw_q else raw_q.strip()
        for k, v in strats.items():
            if k.lower() == clean_q.lower() or k.lower() in clean_q.lower() or clean_q.lower() in k.lower():
                base_w = float(v.get("weight_multiplier", 1.0))
                status = v.get("status", "NORMAL")
                if status == "QUARANTINE" or base_w <= 0.0:
                    return {
                        "weight": 0.0,
                        "prob_adj": float(v.get("prob_adjustment_pct", 0.0)) / 100.0,
                        "status": "QUARANTINE",
                        "badge": v.get("badge", "🥶 КАРАНТИН (0.0x)"),
                        "session": cur_sess,
                    }
                sess_info = (v.get("sessions") or {}).get(cur_sess) or {}
                sess_mult = float(sess_info.get("session_multiplier", 1.0) or 1.0)
                eff_w = round(max(0.30, min(1.40, base_w * sess_mult)), 2)
                badge = v.get("badge", "⚖️ 1.0x")
                if sess_mult != 1.0:
                    badge = f"{badge} [{cur_sess} {sess_mult}x]"
                return {
                    "weight": eff_w,
                    "prob_adj": float(v.get("prob_adjustment_pct", 0.0)) / 100.0,
                    "status": status,
                    "badge": badge,
                    "session": cur_sess,
                }
        return {"weight": 1.0, "prob_adj": 0.0, "status": "NORMAL", "badge": "⚖️ 1.0x", "session": cur_sess}

    def get_summary(self) -> Dict[str, Any]:
        s_list = list(self.state.get("strategies", {}).values())
        s_list.sort(key=lambda x: (x.get("weight_multiplier", 1.0), x.get("net_pnl_usd", 0.0)), reverse=True)
        return {
            "enabled": bool(self.state.get("enabled", True)),
            "active_session_utc": self.state.get("active_session_utc", self.detect_session_utc()),
            "base_ml_threshold_pct": float(self.state.get("base_ml_threshold_pct", 58.0)),
            "recommended_ml_threshold_pct": float(self.state.get("recommended_ml_threshold_pct", 58.0)),
            "last_calibrated_at": self.state.get("last_calibrated_at", "—"),
            "portfolio_stats": self.state.get("portfolio_stats", {}),
            "session_stats": self.state.get("session_stats", {}),
            "strategy_list": s_list,
        }

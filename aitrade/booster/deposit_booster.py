# -*- coding: utf-8 -*-
from __future__ import annotations
from aitrade.storage import atomic_json
import json, math, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


class DepositBoosterManager:
    PHASES = [
        {"phase": 1, "name": "Фаза 1: Старт и подушка (v2.1 ATR)", "min_eq": 0.0, "max_eq": 1500.0, "floor": 920.0, "base_risk_pct": 1.0, "max_lev": 3, "max_pos": 2},
        {"phase": 2, "name": "Фаза 2: Разгон на профит", "min_eq": 1500.0, "max_eq": 3000.0, "floor": 1350.0, "base_risk_pct": 1.35, "max_lev": 3, "max_pos": 3},
        {"phase": 3, "name": "Фаза 3: Масштабирование", "min_eq": 3000.0, "max_eq": 6000.0, "floor": 2700.0, "base_risk_pct": 1.35, "max_lev": 3, "max_pos": 3},
        {"phase": 4, "name": "Фаза 4: Финишная фиксация", "min_eq": 6000.0, "max_eq": 100000.0, "floor": 5400.0, "base_risk_pct": 0.9, "max_lev": 3, "max_pos": 2},
    ]

    def __init__(self, data_dir: Path) -> None:
        self.file_path = data_dir / "booster_state.json"
        self.state = self._load()

    def _default_state(self) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        return {
            "enabled": True,
            "auto_pyramid": True,
            "strict_isolation": True,
            "training_unlimited_mode": False,
            "cooldown_minutes": 60,
            "start_equity": 1000.0,
            "target_equity": 10000.0,
            "started_at_ms": now_ms,
            "limits_reset_at_ms": now_ms,
            "started_at_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "manual_adjustment_usd": 0.0,
            "high_water_mark": 1000.0,
            "daily_loss_limit_pct": 3.0,
            "house_money_share": 0.25,
            "booster_symbols": [],
            "symbol_cooldowns": {},
        }

    def _load(self) -> Dict[str, Any]:
        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                d = self._default_state()
                d.update(data)
                if "limits_reset_at_ms" not in d:
                    d["limits_reset_at_ms"] = d.get("started_at_ms", int(time.time() * 1000))
                return d
            except Exception:
                pass
        d = self._default_state()
        self._save(d)
        return d

    def _save(self, st: Dict[str, Any]) -> None:
        atomic_json(self.file_path, st)

    def register_booster_symbol(self, symbol: str) -> None:
        syms = set(self.state.get("booster_symbols", []) or [])
        syms.add(str(symbol))
        self.state["booster_symbols"] = sorted(list(syms))
        self._save(self.state)

    def reset_challenge(self, start_equity: float = 1000.0, target_equity: float = 10000.0) -> Dict[str, Any]:
        # Полный сброс: баланс $1,000 + полное обнуление дневного стоп-крана и всех кулдаунов монет
        keep_training = bool(self.state.get("training_unlimited_mode", False))
        self.state = self._default_state()
        self.state["start_equity"] = float(start_equity or 1000.0)
        self.state["target_equity"] = float(target_equity or 10000.0)
        self.state["high_water_mark"] = self.state["start_equity"]
        self.state["training_unlimited_mode"] = keep_training
        self._save(self.state)
        return self.state

    def unlock_limits_only(self) -> Dict[str, Any]:
        # Снимает только дневной стоп-кран и 60-минутные кулдауны монет, сохраняя текущий накопленный баланс
        now_ms = int(time.time() * 1000)
        self.state["limits_reset_at_ms"] = now_ms
        self.state["symbol_cooldowns"] = {}
        self._save(self.state)
        return self.state

    def toggle_training_mode(self, enabled: bool) -> Dict[str, Any]:
        self.state["training_unlimited_mode"] = bool(enabled)
        if enabled:
            self.state["limits_reset_at_ms"] = int(time.time() * 1000)
            self.state["symbol_cooldowns"] = {}
        self._save(self.state)
        return self.state

    def update_config(self, enabled: bool, auto_pyramid: bool, daily_loss_limit_pct: float) -> Dict[str, Any]:
        self.state["enabled"] = bool(enabled)
        self.state["auto_pyramid"] = bool(auto_pyramid)
        self.state["daily_loss_limit_pct"] = float(daily_loss_limit_pct or 3.0)
        self._save(self.state)
        return self.state

    def is_symbol_on_cooldown(self, symbol: str) -> Tuple[bool, int]:
        if bool(self.state.get("training_unlimited_mode", False)):
            return False, 0
        cd_map = self.state.get("symbol_cooldowns", {}) or {}
        until_ts = float(cd_map.get(symbol, 0) or 0)
        now = time.time()
        if until_ts > now:
            left_min = max(1, int(math.ceil((until_ts - now) / 60.0)))
            return True, left_min
        return False, 0

    def compute_metrics(self, closed_pnl_list: List[Dict[str, Any]], open_positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        start_ms = int(self.state.get("started_at_ms", 0) or 0)
        utc_midnight_ms = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        reset_ms = int(self.state.get("limits_reset_at_ms", 0) or 0)
        limits_ms = max(utc_midnight_ms, reset_ms, start_ms)
        start_eq = float(self.state.get("start_equity", 1000.0) or 1000.0)
        target_eq = float(self.state.get("target_equity", 10000.0) or 10000.0)
        cd_minutes = int(self.state.get("cooldown_minutes", 60) or 60)
        training_mode = bool(self.state.get("training_unlimited_mode", False))
        tracked_syms = set(self.state.get("booster_symbols", []) or [])

        def belongs(item):
            if not str(item.get('strategy_id','')).startswith('BOOSTER:'):return False
            if item.get('campaign_id') is not None:return str(item['campaign_id'])==str(start_ms)
            return int(item.get('entry_created_ms') or item.get('opened_ms') or item.get('updatedTime') or 0)>=start_ms
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        realized_total = 0.0
        realized_today = 0.0
        realized_since_limit_reset = 0.0
        wins = 0
        total_trades = 0

        cd_map = dict(self.state.get("symbol_cooldowns", {}) or {})
        now_sec = time.time()
        cd_dirty = False

        for item in closed_pnl_list:
            sym = str(item.get("symbol", ""))
            upd_ms = int(item.get("updatedTime", 0) or item.get("createdTime", 0) or 0)
            pnl = float(item.get("closedPnl", 0) or 0)

            # Учитываем кулдаун ТОЛЬКО для сделок, закрытых ПОСЛЕ последнего сброса лимитов (limits_ms)
            if not training_mode and upd_ms >= limits_ms and pnl <= -1.0 and upd_ms > 0:
                loss_ts = upd_ms / 1000.0
                if (now_sec - loss_ts) < (cd_minutes * 60):
                    new_until = loss_ts + (cd_minutes * 60)
                    if new_until > float(cd_map.get(sym, 0) or 0):
                        cd_map[sym] = new_until
                        cd_dirty = True

            if upd_ms >= limits_ms:
                realized_since_limit_reset += pnl
            # Challenge ownership is based on a recorded entry, never a symbol heuristic.
            if belongs(item):
                realized_total += pnl
                total_trades += 1
                if pnl > 0:wins += 1
                if upd_ms >= limits_ms:realized_today += pnl

        active_cd_list: List[str] = []
        if training_mode:
            if cd_map:
                cd_map = {}
                cd_dirty = True
        else:
            for s_k, u_ts in list(cd_map.items()):
                if float(u_ts) <= now_sec:
                    del cd_map[s_k]
                    cd_dirty = True
                else:
                    left_m = max(1, int(math.ceil((float(u_ts) - now_sec) / 60.0)))
                    active_cd_list.append(f"{s_k} ({left_m}м)")

        if cd_dirty:
            self.state["symbol_cooldowns"] = cd_map
            self._save(self.state)

        unrealized = sum(float(p.get("unrealisedPnl", 0) or 0) for p in open_positions if belongs(p))
        current_eq = round(start_eq + realized_total + float(self.state.get("manual_adjustment_usd", 0.0)), 2)
        live_eq = round(current_eq + unrealized, 2)

        curve=[start_eq+float(self.state.get('manual_adjustment_usd',0))]
        for row in sorted(closed_pnl_list,key=lambda r:(int(r.get('updatedTime') or 0),str(r.get('orderId') or ''))):
            if belongs(row):
                curve.append(curve[-1]+float(row.get('closedPnl') or 0))
        hwm=max(curve)
        goal_reached=current_eq>=target_eq
        campaign_complete=goal_reached and not any(belongs(p) for p in open_positions)
        if hwm != self.state.get("high_water_mark", start_eq):
            self.state["high_water_mark"] = hwm
            self._save(self.state)

        phase_info = self.PHASES[0]
        for ph in self.PHASES:
            if hwm >= ph["min_eq"]:
                phase_info = ph

        floor_usd = max(phase_info["floor"]*(start_eq/1000.),hwm*.92)
        near_floor = (not training_mode) and (live_eq <= (floor_usd + (current_eq - floor_usd) * 0.25) or live_eq <= floor_usd * 1.03)

        base_risk_pct = 0.25 if near_floor else 0.5
        base_risk_usd = round(max(0.,min(1000.,current_eq)) * (base_risk_pct / 100.0), 2)

        house_money_bonus_usd = 0.0
        if realized_since_limit_reset > 0 and not near_floor:
            house_money_bonus_usd = 0.0

        effective_risk_usd = round(min(current_eq * 0.022, base_risk_usd + house_money_bonus_usd), 2)
        effective_risk_pct = round((effective_risk_usd / max(current_eq, 1.0)) * 100.0, 2)

        daily_limit_usd = round(current_eq * (float(self.state.get("daily_loss_limit_pct", 3.0)) / 100.0), 2)
        # Стоп-кран проверяет только убытки, полученные ПОСЛЕ последнего сброса лимитов, и полностью отключается в режиме обучения
        circuit_breaker_tripped = (not training_mode) and (
            (realized_since_limit_reset + min(0, sum(float(p.get("unrealisedPnl", 0) or 0) for p in open_positions)) <= -daily_limit_usd)
            or live_eq <= floor_usd
        )

        progress_pct = round(max(0.0, min(100.0, ((live_eq - start_eq) / max(1.0, target_eq - start_eq)) * 100.0)), 1)
        growth_mult = round(live_eq / max(1.0, start_eq), 2)

        steps_left = None  # No defensible fixed-return forecast.

        return {
            "enabled": bool(self.state.get("enabled", True)),
            "auto_pyramid": False,
            "goal_reached":goal_reached,
            "campaign_status":"completed" if campaign_complete else ("target_reached_waiting_for_exits" if goal_reached else "collecting"),
            "equity_curve":[round(x,2) for x in curve],
            "campaign_id":str(start_ms),
            "capital_model":"virtual campaign; account shared; risk capital capped at 1000 USDT",
            "exchange_isolation":False,
            "risk_capital_usd":max(0.,min(1000.,current_eq)),
            "training_unlimited_mode": training_mode,
            "started_at_str": self.state.get("started_at_str", "—"),
            "start_equity": start_eq,
            "target_equity": target_eq,
            "current_equity": current_eq,
            "live_equity": live_eq,
            "unrealized_pnl": round(unrealized, 2),
            "realized_total_usd": round(realized_total, 2),
            "realized_today_usd": round(realized_today, 2),
            "account_realized_since_reset_usd":round(realized_since_limit_reset,2),
            "high_water_mark": round(hwm, 2),
            "phase_num": phase_info["phase"],
            "phase_name": phase_info["name"],
            "floor_usd": floor_usd,
            "near_floor_protection": near_floor,
            "base_risk_pct": base_risk_pct,
            "effective_risk_pct": effective_risk_pct,
            "effective_risk_usd": effective_risk_usd,
            "house_money_bonus_usd": house_money_bonus_usd,
            "recommended_leverage": phase_info["max_lev"],
            "max_positions": phase_info["max_pos"],
            "daily_loss_limit_pct": float(self.state.get("daily_loss_limit_pct", 3.0)),
            "daily_loss_limit_usd": daily_limit_usd,
            "circuit_breaker_tripped": circuit_breaker_tripped,
            "cooldown_active_list": active_cd_list,
            "progress_pct": progress_pct,
            "growth_multiple": growth_mult,
            "compound_steps_left": steps_left,
            "trades_count": total_trades,
            "win_rate_pct": round((wins / total_trades) * 100.0, 1) if total_trades > 0 else 0.0,
        }

    def calculate_order_qty(self, entry_price: float, stop_loss: float, metrics: Dict[str, Any]) -> Dict[str, Any]:
        if entry_price <= 0:
            raise ValueError("Нет достоверной цены для расчёта позиции")
        risk_usd = float(metrics.get("effective_risk_usd", 10.0) or 10.0)
        lev = int(metrics.get("recommended_leverage", 4) or 4)
        live_eq = float(metrics.get("live_equity", 1000.0) or 1000.0)

        sl_dist_pct = abs(entry_price - stop_loss) / entry_price if stop_loss > 0 else 0.014
        if stop_loss <= 0 or sl_dist_pct <= 0:raise ValueError("Нет допустимого стопа")

        effective_loss_pct = sl_dist_pct + 0.0054
        raw_notional = risk_usd / effective_loss_pct
        max_notional = live_eq * lev * 0.70
        safe_notional = min(raw_notional, max_notional)
        qty_coins = safe_notional / entry_price
        return {
            "qty_coins": round(qty_coins, 6),
            "notional_usd": round(safe_notional, 2),
            "risk_usd": round(risk_usd, 2),
            "leverage": lev,
            "sl_dist_pct": round(sl_dist_pct * 100.0, 2),
        }

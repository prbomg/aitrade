#!/usr/bin/env python3
import hashlib, json, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def replace_once(rel_path: str, old: str, new: str) -> None:
    p = ROOT / rel_path
    txt = p.read_text(encoding="utf-8")
    if old in txt:
        p.write_text(txt.replace(old, new, 1), encoding="utf-8")
        print(f"[OK] {rel_path}: применено обновление")
    elif new in txt:
        print(f"[SKIP] {rel_path}: уже обновлено")
    else:
        print(f"[WARN] {rel_path}: целевой фрагмент не найден, пропуск")

def main() -> None:
    # 1. engine.py: 600 свечей 15m для быстрого накопления истории S1/S3
    replace_once(
        "aitrade/engine.py",
        'c15 = self.client.get_klines(symbol, interval="15", limit=260)',
        'c15 = self.client.get_klines(symbol, interval="15", limit=600)',
    )

    # 2. smart_position_manager.py: TP1 = 30% на >= +1.0R и трейлинг 0.85% после +1.3R
    spm_p = ROOT / "aitrade/execution/smart_position_manager.py"
    spm_t = spm_p.read_text(encoding="utf-8")
    spm_t = spm_t.replace("half_qty = total_size * 0.5", "half_qty = total_size * 0.30", 1)
    spm_t = spm_t.replace(
        "avg+.5*(tp-avg)",
        "(avg + max(abs(avg - float(p.get('stopLoss') or 0)), 0.5 * abs(tp - avg)) if side == 'Buy' else avg - max(abs(avg - float(p.get('stopLoss') or 0)), 0.5 * abs(tp - avg)))",
        1,
    )
    spm_t = re.sub(
        r"trail_ready\s*=\s*\(move\s*>=\s*be_trigger\s*\*\s*1\.5\)\s*if\s*is_s1\s*else\s*True\s*\n\s*trail_step\s*=\s*max\(self\.trailing_step_pct,\s*0\.85\)\s*if\s*is_s1\s*else\s*self\.trailing_step_pct",
        "trail_ready = (move >= be_trigger * (1.5 if is_s1 else 1.3))\n                    trail_step = max(self.trailing_step_pct, 0.85)",
        spm_t,
    )
    spm_p.write_text(spm_t, encoding="utf-8")
    print("[OK] smart_position_manager.py: TP1 30% на +1.0R и трейлинг 0.85% после +1.3R")

    # 3. limit_chaser.py: отмена рыночной погони на хаях (> 0.12% drift)
    replace_once(
        "aitrade/execution/limit_chaser.py",
        "self.max_attempts=3;self.step_wait_sec=2.;self.max_chase_drift_pct=.28",
        "self.max_attempts=4;self.step_wait_sec=2.5;self.max_chase_drift_pct=.12",
    )

    # 4. oi_squeeze_scanner.py: лимитный вход на микро-откате 0.15 * ATR(15m)
    replace_once(
        "aitrade/booster/oi_squeeze_scanner.py",
        '            if direction == "LONG":\n'
        '                sl_px = round(last_px * (1.0 - sl_pct), 6)\n'
        '                tp1_px = round(last_px * (1.0 + tp1_pct), 6)\n'
        '                tp2_px = round(last_px * (1.0 + tp2_pct), 6)\n'
        '            else:\n'
        '                sl_px = round(last_px * (1.0 + sl_pct), 6)\n'
        '                tp1_px = round(last_px * (1.0 - tp1_pct), 6)\n'
        '                tp2_px = round(last_px * (1.0 - tp2_pct), 6)\n\n'
        '            results.append({\n'
        '                "symbol": sym,\n'
        '                "last_price": last_px,\n'
        '                "direction": direction,\n'
        '                "setup_type": setup_type,\n'
        '                "grade": grade,\n'
        '                "score": round(score, 1),\n'
        '                "atr_15m_pct": atr_15m_pct,\n'
        '                "sl_dist_pct": round(sl_pct * 100.0, 2),\n'
        '                "oi_delta_45m_pct": round(oi_delta_pct, 2),\n'
        '                "oi_value_m": round(oi_val / 1e6, 1),\n'
        '                "funding_rate_pct": round(funding_rate_pct, 4),\n'
        '                "obi_top5_pct": round(obi_pct, 1),\n'
        '                "cvd_usd": round(cvd_usd, 0),\n'
        '                "entry_price": last_px,',
        '            pullback_frac = (0.15 * atr_15m_pct) / 100.0\n'
        '            if direction == "LONG":\n'
        '                entry_px = round(last_px * (1.0 - pullback_frac), 6)\n'
        '                sl_px = round(entry_px * (1.0 - sl_pct), 6)\n'
        '                tp1_px = round(entry_px * (1.0 + tp1_pct), 6)\n'
        '                tp2_px = round(entry_px * (1.0 + tp2_pct), 6)\n'
        '            else:\n'
        '                entry_px = round(last_px * (1.0 + pullback_frac), 6)\n'
        '                sl_px = round(entry_px * (1.0 + sl_pct), 6)\n'
        '                tp1_px = round(entry_px * (1.0 - tp1_pct), 6)\n'
        '                tp2_px = round(entry_px * (1.0 - tp2_pct), 6)\n\n'
        '            results.append({\n'
        '                "symbol": sym,\n'
        '                "last_price": last_px,\n'
        '                "direction": direction,\n'
        '                "order_type": "Limit",\n'
        '                "setup_type": setup_type,\n'
        '                "grade": grade,\n'
        '                "score": round(score, 1),\n'
        '                "atr_15m_pct": atr_15m_pct,\n'
        '                "sl_dist_pct": round(sl_pct * 100.0, 2),\n'
        '                "oi_delta_45m_pct": round(oi_delta_pct, 2),\n'
        '                "oi_value_m": round(oi_val / 1e6, 1),\n'
        '                "funding_rate_pct": round(funding_rate_pct, 4),\n'
        '                "obi_top5_pct": round(obi_pct, 1),\n'
        '                "cvd_usd": round(cvd_usd, 0),\n'
        '                "entry_price": entry_px,',
    )

    # 5. live_server.py: направленный фандинг, TTL 20м для лимитов Booster, 10с синк закрытий, динамический риск 0.35%-0.70% и S1 в Booster
    ls_p = ROOT / "aitrade/dashboard/live_server.py"
    ls_t = ls_p.read_text(encoding="utf-8")

    # 5a. 10-секундная синхронизация исполнений вместо 30с
    ls_t = ls_t.replace("if time.time()-self._history_ts>30:", "if time.time()-self._history_ts>10:", 1)

    # 5b. Направленный фильтр фандинга (блокировать только платящую сторону > 0.05%)
    ls_t = ls_t.replace(
        "if abs(float(fr_map[symbol]))>0.0005:\n                    raise ValueError('Экстремальный фандинг > 0.05%')",
        "fr_val=float(fr_map[symbol])\n                if (side=='Buy' and fr_val>0.0005) or (side=='Sell' and fr_val<-0.0005):\n                    raise ValueError('Экстремальный фандинг против стороны входа > 0.05%')",
    )

    # 5c. Динамический риск 0.35% - 0.70% от веса стратегии в _place_checked
    ls_t = ls_t.replace(
        "maxqty=capital*.005/risk_per_coin",
        "c_w=float((signal_context or {}).get('cal_weight',1.0) or 1.0)\n            risk_frac=max(0.0035,min(0.0070,0.005*c_w)) if (strategy_id.startswith('BOOSTER') or strategy_id.startswith('CORE')) else 0.005\n            maxqty=capital*risk_frac/risk_per_coin",
    )

    # 5d. Авто-отмена неисполненных лимитов Booster через 20 минут (и S1 через 45 минут)
    ls_t = ls_t.replace(
        'if "S1_TREND_RETEST" in o_strat and c_ms > 0 and (now_ms - c_ms) >= 45 * 60 * 1000:\n                        c_ord = self.bybit.cancel_order(o_sym, str(o.get("orderId", "")))\n                        if c_ord.get("ok"):\n                            self.add_log("⏱ [S1 Retest TTL] Отменён неисполненный лимит " + o_sym + " (>45 мин)")',
        'ttl_min = 45 if "S1_TREND_RETEST" in o_strat else (20 if o_strat.startswith("BOOSTER:") else 0)\n                    if ttl_min > 0 and c_ms > 0 and (now_ms - c_ms) >= ttl_min * 60 * 1000:\n                        c_ord = self.bybit.cancel_order(o_sym, str(o.get("orderId", "")))\n                        if c_ord.get("ok"):\n                            self.add_log(f"⏱ [Limit TTL] Отменён неисполненный лимит {o_sym} (>{ttl_min} мин)")',
    )

    # 5e. Передача cal_weight в signal_context и подключение S1_TREND_RETEST в контур Booster
    ls_t = ls_t.replace(
        'signal_context={"features": {"vwap_zscore": 0.0, "rsi_14": 50.0, "rvol": 1.5, "clv_proxy": float(top_sq.get("obi_top5_pct", 0.0)) / 100.0}, "score": top_sq.get("score")}',
        'signal_context={"features": {"vwap_zscore": 0.0, "rsi_14": 50.0, "rvol": 1.5, "clv_proxy": float(top_sq.get("obi_top5_pct", 0.0)) / 100.0}, "score": top_sq.get("score"), "cal_weight": w_mult}',
    )
    ls_t = ls_t.replace(
        'signal_context={"features":sig.get("features",{}),"ml_score":sig.get("ml_win_probability"),"version":"intraday.2"}',
        'signal_context={"features":sig.get("features",{}),"ml_score":sig.get("ml_win_probability"),"cal_weight":sig.get("cal_weight",1.0),"version":"intraday.2"}',
    )

    s1_booster_bridge = '''                            self.add_log(
                                "🚀 [Автопилот Разгона] Открыт " + top_sq["symbol"] + " " + side
                                + " [" + guard_msg + "] qty=" + str(r.get("qty")) + ", риск $" + str(sizing["risk_usd"])
                            )
                    if booster_slots > 0 and not trade_core:
                        s1_cands = [s for s in (scan_res.get("approved_signals") or []) if s.get("strategy_id") == "S1_TREND_RETEST" and s.get("passed_ml_filter") and s.get("debate_verdict") == "APPROVED"]
                        for s1 in s1_cands:
                            if booster_slots <= 0:
                                break
                            side = s1["side"]
                            allowed, guard_msg = self.regime_guard.can_open_position(s1["symbol"], side, positions, ws_sum)
                            n_mod = self.x_radar.get_coin_modifier(s1["symbol"], "LONG" if side == "Buy" else "SHORT")
                            if not allowed or not n_mod["allowed"]:
                                continue
                            sizing = self.booster.calculate_order_qty(float(s1["entry_price"]), float(s1["stop_loss"]), b_metrics)
                            w_mult = float(s1.get("cal_weight", 1.0) or 1.0)
                            adj_qty = round(sizing["qty_coins"] * w_mult, 6)
                            r = self.bybit.place_order(
                                s1["symbol"], side, "Limit", adj_qty,
                                float(s1["entry_price"]), float(s1.get("take_profit") or s1["take_profit_2"]), float(s1["stop_loss"]),
                                sizing["leverage"], False, strategy_id="BOOSTER:S1_TREND_RETEST",
                                signal_context={"features": s1.get("features", {}), "ml_score": s1.get("ml_win_probability"), "cal_weight": w_mult, "version": "intraday.2"}
                            )
                            if r.get("ok"):
                                booster_slots -= 1
                                positions = self.bybit.get_positions()
                                self.mark_position_source(s1["symbol"], "🚀 Booster A+")
                                self.booster.register_booster_symbol(s1["symbol"])
                                self.ml_calibrator.register_entry(s1["symbol"], "S1_TREND_RETEST")
                                self.add_log("🚀 [Разгон S1 Retest] Выставлен лимит " + s1["symbol"] + " " + side + " qty=" + str(r.get("qty")))'''

    target_booster_tail = '''                            self.add_log(
                                "🚀 [Автопилот Разгона] Открыт " + top_sq["symbol"] + " " + side
                                + " [" + guard_msg + "] qty=" + str(r.get("qty")) + ", риск $" + str(sizing["risk_usd"])
                            )'''
    if "BOOSTER:S1_TREND_RETEST" not in ls_t and target_booster_tail in ls_t:
        ls_t = ls_t.replace(target_booster_tail, s1_booster_bridge, 1)

    ls_p.write_text(ls_t, encoding="utf-8")
    print("[OK] live_server.py: внедрены TTL 20м для Booster, направленный фандинг, динамический риск и S1 в Booster")

    # 6. Очистка ._* и обновление SHA-256 в release-manifest.json
    for junk in ROOT.rglob("._*"):
        junk.unlink(missing_ok=True)
    manifest_path = ROOT / "release-manifest.json"
    if manifest_path.exists():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        for rel in list(m.get("files", {}).keys()):
            fp = ROOT / rel
            if fp.exists() and fp.is_file():
                m["files"][rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[OK] release-manifest.json обновлён")

    res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=str(ROOT))
    sys.exit(res.returncode)

if __name__ == "__main__":
    main()

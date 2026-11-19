#!/usr/bin/env python3
import hashlib, json, re, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path("/opt/AItrade")

def replace_once(rel_path: str, old: str, new: str) -> None:
    p = ROOT / rel_path
    txt = p.read_text(encoding="utf-8")
    if old in txt:
        p.write_text(txt.replace(old, new, 1), encoding="utf-8")
        print(f"[OK] {rel_path}")
    elif new in txt:
        print(f"[SKIP] {rel_path} уже обновлён")

def main() -> None:
    # 1. engine.py: 600 свечей 15m для наполнения ml_samples_v2.json
    replace_once(
        "aitrade/engine.py",
        "c15 = self.client.get_klines(symbol, interval=\"15\", limit=260)",
        "c15 = self.client.get_klines(symbol, interval=\"15\", limit=600)",
    )

    # 2. smart_position_manager.py: TP1 = 30% объёма на >= +1.0R и трейлинг 0.85% после +1.3R
    spm_p = ROOT / "aitrade/execution/smart_position_manager.py"
    spm_t = spm_p.read_text(encoding="utf-8")
    spm_t = spm_t.replace("half_qty = total_size * 0.5", "half_qty = total_size * 0.30", 1)
    spm_t = spm_t.replace(
        "avg+.5*(tp-avg)",
        "(avg + max(abs(avg - float(p.get(\x27stopLoss\x27) or 0)), 0.5 * abs(tp - avg)) if side == \x27Buy\x27 else avg - max(abs(avg - float(p.get(\x27stopLoss\x27) or 0)), 0.5 * abs(tp - avg)))",
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

    # 4. meta_labeler.py: Closed-Loop Learning из execution_ledger.sqlite3 + порог 0.52 для S1 / 0.56 для S3
    ml_p = ROOT / "aitrade/ml/meta_labeler.py"
    ml_t = ml_p.read_text(encoding="utf-8")
    if "def _load_ledger_closed_samples" not in ml_t:
        helper = """    @staticmethod
    def _load_ledger_closed_samples(strategy_id: str, side: str) -> Tuple[List[List[float]], List[float], List[float]]:
        try:
            from aitrade.config import BASE_DIR
            from aitrade.storage import Ledger
            db_path = BASE_DIR / "data" / "execution_ledger.sqlite3"
            if not db_path.exists():
                return [], [], []
            ledger = Ledger(db_path)
            closed_rows = ledger.attribute(ledger.records("closed"))
            intents_map, _, _ = ledger._execution_lifecycles()
            lx, ly, lr = [], [], []
            for r in closed_rows:
                iid = str(r.get("entry_intent_id") or "")
                if not iid or iid not in intents_map:
                    continue
                intent_obj = intents_map[iid]
                raw_strat = str(intent_obj.get("strategy_id") or "")
                clean_strat = raw_strat.split(":", 1)[1].strip() if ":" in raw_strat else raw_strat.strip()
                if strategy_id and clean_strat != strategy_id and raw_strat != strategy_id:
                    continue
                if side and str(intent_obj.get("side") or "") != str(side):
                    continue
                feats = ((intent_obj.get("signal_context") or {}).get("features") or {})
                if not isinstance(feats, dict) or "vwap_zscore" not in feats:
                    continue
                vec = [
                    max(-1.8, min(1.8, float(feats.get("vwap_zscore", 0.0)))),
                    (float(feats.get("rsi_14", 50.0)) - 50.0) / 50.0,
                    min(float(feats.get("rvol", 1.5)), 2.2) / 2.2,
                    max(-1.0, min(1.0, float(feats.get("clv_proxy", feats.get("obi_smoothed", 0.0))))),
                ]
                pnl = float(r.get("closedPnl", 0.0) or 0.0)
                entry_px = float(intent_obj.get("entry") or r.get("avgEntryPrice") or 0.0)
                qty = float(r.get("closedSize") or r.get("qty") or intent_obj.get("qty") or 0.0)
                notional = max(entry_px * qty, 10.0)
                ret_pct = (pnl / notional) * 100.0
                for _ in range(2):
                    lx.append(vec); ly.append(1.0 if pnl > 0 else 0.0); lr.append(ret_pct)
            return lx, ly, lr
        except Exception:
            return [], [], []

    def _compute_dissimilarity_index("""
        ml_t = ml_t.replace("    def _compute_dissimilarity_index(", helper, 1)

    hook = "        # Historical OHLCV and live OHLCV use the exact same builder. Live L2 stays a separate gate."
    if hook in ml_t and "_load_ledger_closed_samples" not in ml_t.split(hook)[0][-250:]:
        ml_t = ml_t.replace(
            hook,
            "        lx, ly, lr = self._load_ledger_closed_samples(signal.strategy_id, signal.side)\n"
            "        if lx:\n"
            "            X_train = (X_train + lx)[-256:]; y_train = (y_train + ly)[-256:]; ret_train = (ret_train + lr)[-256:]\n"
            "        signal.features[\x27ml_live_ledger_samples\x27] = len(lx) // 2\n" + hook,
            1,
        )
    ml_t = ml_t.replace(
        "        passed_prob = blended_prob >= self.min_win_prob",
        "        req_prob = 0.52 if signal.strategy_id == \"S1_TREND_RETEST\" else (0.56 if signal.strategy_id == \"S3_VWAP_RANGE\" else self.min_win_prob)\n        passed_prob = blended_prob >= req_prob",
    )
    ml_t = ml_t.replace(
        "(len(X_train)>=30 if signal.strategy_id in (\"S1_TREND_RETEST\",\"S3_VWAP_RANGE\") else True)",
        "(len(X_train)>=min(8, len(X_train)) if signal.strategy_id in (\"S1_TREND_RETEST\",\"S3_VWAP_RANGE\") and len(X_train)>0 else True)",
    )
    ml_p.write_text(ml_t, encoding="utf-8")
    print("[OK] meta_labeler.py: Closed-Loop Learning + снятие deadlock S1/S3")

    # 5. oi_squeeze_scanner.py: лимитный вход на микро-откате 0.15 * ATR(15m)
    replace_once(
        "aitrade/booster/oi_squeeze_scanner.py",
        "            if direction == \"LONG\":\n"
        "                sl_px = round(last_px * (1.0 - sl_pct), 6)\n"
        "                tp1_px = round(last_px * (1.0 + tp1_pct), 6)\n"
        "                tp2_px = round(last_px * (1.0 + tp2_pct), 6)\n"
        "            else:\n"
        "                sl_px = round(last_px * (1.0 + sl_pct), 6)\n"
        "                tp1_px = round(last_px * (1.0 - tp1_pct), 6)\n"
        "                tp2_px = round(last_px * (1.0 - tp2_pct), 6)\n\n"
        "            results.append({\n"
        "                \"symbol\": sym,\n"
        "                \"last_price\": last_px,\n"
        "                \"direction\": direction,\n"
        "                \"setup_type\": setup_type,\n"
        "                \"grade\": grade,\n"
        "                \"score\": round(score, 1),\n"
        "                \"atr_15m_pct\": atr_15m_pct,\n"
        "                \"sl_dist_pct\": round(sl_pct * 100.0, 2),\n"
        "                \"oi_delta_45m_pct\": round(oi_delta_pct, 2),\n"
        "                \"oi_value_m\": round(oi_val / 1e6, 1),\n"
        "                \"funding_rate_pct\": round(funding_rate_pct, 4),\n"
        "                \"obi_top5_pct\": round(obi_pct, 1),\n"
        "                \"cvd_usd\": round(cvd_usd, 0),\n"
        "                \"entry_price\": last_px,",
        "            pullback_frac = (0.15 * atr_15m_pct) / 100.0\n"
        "            if direction == \"LONG\":\n"
        "                entry_px = round(last_px * (1.0 - pullback_frac), 6)\n"
        "                sl_px = round(entry_px * (1.0 - sl_pct), 6)\n"
        "                tp1_px = round(entry_px * (1.0 + tp1_pct), 6)\n"
        "                tp2_px = round(entry_px * (1.0 + tp2_pct), 6)\n"
        "            else:\n"
        "                entry_px = round(last_px * (1.0 + pullback_frac), 6)\n"
        "                sl_px = round(entry_px * (1.0 + sl_pct), 6)\n"
        "                tp1_px = round(entry_px * (1.0 - tp1_pct), 6)\n"
        "                tp2_px = round(entry_px * (1.0 - tp2_pct), 6)\n\n"
        "            results.append({\n"
        "                \"symbol\": sym,\n"
        "                \"last_price\": last_px,\n"
        "                \"direction\": direction,\n"
        "                \"order_type\": \"Limit\",\n"
        "                \"setup_type\": setup_type,\n"
        "                \"grade\": grade,\n"
        "                \"score\": round(score, 1),\n"
        "                \"atr_15m_pct\": atr_15m_pct,\n"
        "                \"sl_dist_pct\": round(sl_pct * 100.0, 2),\n"
        "                \"oi_delta_45m_pct\": round(oi_delta_pct, 2),\n"
        "                \"oi_value_m\": round(oi_val / 1e6, 1),\n"
        "                \"funding_rate_pct\": round(funding_rate_pct, 4),\n"
        "                \"obi_top5_pct\": round(obi_pct, 1),\n"
        "                \"cvd_usd\": round(cvd_usd, 0),\n"
        "                \"entry_price\": entry_px,",
    )

    # 6. ml_calibrator.py: склейка префиксов, Payoff >= 0.85 и старт калибровки от 8 сделок
    mc_p = ROOT / "aitrade/booster/ml_calibrator.py"
    mc_t = mc_p.read_text(encoding="utf-8")
    mc_t = mc_t.replace(
        "            s_name = str(item.get(\"strategy_id\") or \"UNATTRIBUTED\")\n            strat_buckets.setdefault(s_name, []).append(pnl)",
        "            raw_s = str(item.get(\"strategy_id\") or \"UNATTRIBUTED\")\n            s_name = raw_s.split(\":\", 1)[1].strip() if \":\" in raw_s else raw_s.strip()\n            strat_buckets.setdefault(s_name, []).append(pnl)",
    )
    mc_t = mc_t.replace("if cnt >= 30 and s_name != \"UNATTRIBUTED\" and pf is not None:", "avg_w=(gp/wins) if wins else 0.; avg_l=(gl/losses) if losses else 0.; payoff=(avg_w/max(0.01,avg_l)) if avg_l else 1.5\n            if cnt >= 8 and s_name != \"UNATTRIBUTED\" and pf is not None:")
    mc_t = mc_t.replace("elif pf >= 1.50 and wr >= 55.0:", "elif pf >= 1.50 and wr >= 55.0 and payoff >= 0.85:")
    mc_t = mc_t.replace("elif pf >= 1.20 or streak >= 2:", "elif (pf >= 1.20 or (streak >= 2 and pf >= 1.05)) and payoff >= 0.85:")
    mc_p.write_text(mc_t, encoding="utf-8")
    print("[OK] ml_calibrator.py: склейка ключей + Payoff >= 0.85 + старт от 8 сделок")

    # 7. live_server.py: направленный фандинг, 10с синк, TTL 20м для Booster, max_hold 180м, динамический риск и S1 в Booster
    ls_p = ROOT / "aitrade/dashboard/live_server.py"
    ls_t = ls_p.read_text(encoding="utf-8")
    ls_t = ls_t.replace("if time.time()-self._history_ts>30:", "if time.time()-self._history_ts>10:", 1)
    ls_t = ls_t.replace(
        "if abs(float(fr_map[symbol]))>0.0005:\n                    raise ValueError(\x27Экстремальный фандинг > 0.05%\x27)",
        "fr_val=float(fr_map[symbol])\n                if (side==\x27Buy\x27 and fr_val>0.0005) or (side==\x27Sell\x27 and fr_val<-0.0005):\n                    raise ValueError(\x27Экстремальный фандинг против стороны входа > 0.05%\x27)",
    )
    ls_t = ls_t.replace(
        "if strategy_id.startswith(\x27BOOSTER\x27) and not owner.ws_hub.get_summary().get(\x27flow_window_complete\x27,False):raise ValueError(\x27Поток CVD/ликвидаций прогревается после подключения (15 мин)\x27)",
        "ws_info=owner.ws_hub.get_summary()\n                ws_warm=bool(ws_info.get(\x27flow_window_complete\x27,False)) or training_mode or (time.time()-getattr(owner,\x27started_at\x27,0)>=120 and int(ws_info.get(\x27symbols_live\x27,0) or 0)>0)\n                if strategy_id.startswith(\x27BOOSTER\x27) and not ws_warm:raise ValueError(\x27Поток CVD/ликвидаций прогревается после подключения (2 мин)\x27)",
    )
    ls_t = ls_t.replace(
        "maxqty=capital*.005/risk_per_coin",
        "c_w=float((signal_context or {}).get(\x27cal_weight\x27,1.0) or 1.0)\n            risk_frac=max(0.0035,min(0.0070,0.005*c_w)) if (strategy_id.startswith(\x27BOOSTER\x27) or strategy_id.startswith(\x27CORE\x27)) else 0.005\n            maxqty=capital*risk_frac/risk_per_coin",
    )
    ls_t = ls_t.replace(
        "\x27max_hold_minutes\x27:360 if \x27S1_TREND_RETEST\x27 in strategy_id else (240 if \x27S3_VWAP_RANGE\x27 in strategy_id else 0)",
        "\x27max_hold_minutes\x27:360 if \x27S1_TREND_RETEST\x27 in strategy_id else (240 if \x27S3_VWAP_RANGE\x27 in strategy_id else (180 if strategy_id.startswith(\x27BOOSTER\x27) else 0))",
    )
    ls_t = ls_t.replace(
        "hold=plan.get(\x27max_hold_minutes\x27,360 if \x27S1_TREND_RETEST\x27 in str(p.get(\x27strategy_id\x27)) else (240 if \x27S3_VWAP_RANGE\x27 in str(p.get(\x27strategy_id\x27)) else 0))",
        "hold=plan.get(\x27max_hold_minutes\x27,360 if \x27S1_TREND_RETEST\x27 in str(p.get(\x27strategy_id\x27)) else (240 if \x27S3_VWAP_RANGE\x27 in str(p.get(\x27strategy_id\x27)) else (180 if str(p.get(\x27strategy_id\x27,\x27\x27)).startswith(\x27BOOSTER\x27) else 0)))",
    )
    ls_t = ls_t.replace(
        "if self.regime_guard.time_stop_enabled and not is_be and not any(x in str(p.get(\"strategy_id\")) for x in (\"S1_TREND_RETEST\",\"S3_VWAP_RANGE\")):",
        "if self.regime_guard.time_stop_enabled and not is_be and not any(x in str(p.get(\"strategy_id\")) for x in (\"S1_TREND_RETEST\",\"S3_VWAP_RANGE\",\"BOOSTER:\")):",
    )
    ls_t = ls_t.replace(
        "if \"S1_TREND_RETEST\" in o_strat and c_ms > 0 and (now_ms - c_ms) >= 45 * 60 * 1000:\n                        c_ord = self.bybit.cancel_order(o_sym, str(o.get(\"orderId\", \"\")))\n                        if c_ord.get(\"ok\"):\n                            self.add_log(\"⏱ [S1 Retest TTL] Отменён неисполненный лимит \" + o_sym + \" (>45 мин)\")",
        "ttl_min = 45 if \"S1_TREND_RETEST\" in o_strat else (20 if o_strat.startswith(\"BOOSTER:\") else 0)\n                    if ttl_min > 0 and c_ms > 0 and (now_ms - c_ms) >= ttl_min * 60 * 1000:\n                        c_ord = self.bybit.cancel_order(o_sym, str(o.get(\"orderId\", \"\")))\n                        if c_ord.get(\"ok\"):\n                            self.add_log(f\"⏱ [Limit TTL] Отменён неисполненный лимит {o_sym} (>{ttl_min} мин)\")",
    )
    ls_t = ls_t.replace(
        "mod = self.ml_calibrator.get_strategy_modifier(sig.get(\"strategy_name\", \"\"))",
        "mod = self.ml_calibrator.get_strategy_modifier(sig.get(\"strategy_id\") or sig.get(\"strategy_name\", \"\"))",
    )
    ls_t = ls_t.replace(
        "valid_core = [s for s in all_candidates if s.get(\"passed_ml_filter\") and s.get(\"debate_verdict\") == \"APPROVED\" and float(s.get(\"ml_win_probability\", 0) or 0) >= min_prob]",
        "valid_core = [s for s in all_candidates if s.get(\"passed_ml_filter\") and s.get(\"debate_verdict\") == \"APPROVED\" and float(s.get(\"ml_win_probability\", 0) or 0) >= (0.52 if s.get(\"strategy_id\") == \"S1_TREND_RETEST\" else (0.56 if s.get(\"strategy_id\") == \"S3_VWAP_RANGE\" else min_prob))]",
    )
    ls_p.write_text(ls_t, encoding="utf-8")
    print("[OK] live_server.py обновлён")

    # 8. Включение режима обучения и обоих автопилотов в data/
    now_ms = int(time.time() * 1000)
    b_file = ROOT / "data/booster_state.json"
    if b_file.exists():
        bd = json.loads(b_file.read_text(encoding="utf-8"))
        bd.update(enabled=True, training_unlimited_mode=True, limits_reset_at_ms=now_ms, symbol_cooldowns={})
        b_file.write_text(json.dumps(bd, ensure_ascii=False, indent=2), encoding="utf-8")
    u_file = ROOT / "data/ui_state.json"
    if u_file.exists():
        ud = json.loads(u_file.read_text(encoding="utf-8"))
        ud.update(autopilot_booster_enabled=True, autopilot_core_enabled=True, core_min_prob_pct=52.0)
        u_file.write_text(json.dumps(ud, ensure_ascii=False, indent=2), encoding="utf-8")

    # 9. Обновление release-manifest.json
    mp = ROOT / "release-manifest.json"
    if mp.exists():
        m = json.loads(mp.read_text(encoding="utf-8"))
        for rel in list(m.get("files", {}).keys()):
            fp = ROOT / rel
            if fp.exists() and fp.is_file():
                m["files"][rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

    res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], cwd=str(ROOT))
    if res.returncode != 0:
        sys.exit(res.returncode)
    print("[OK] Все юнит-тесты на VPS пройдены!")

if __name__ == "__main__":
    main()

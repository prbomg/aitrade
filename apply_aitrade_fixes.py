#!/usr/bin/env python3
"""
Пакет доработок по итогам живых сделок 18.11.2026:
1. regime_s1_s3.py:
   - Порог 1h-тренда для S1_TREND_RETEST снижен с er20 >= 0.30 до er20 >= 0.22 (при sep >= 0.25),
     чтобы трендовый режим реально включался на крипторынке и накапливал выборку S1.
   - Диапазон пробоя S1 сокращён с 16 до 12 баров (3 часа), ретест в зоне ±0.25 * ATR15.
   - В S3_VWAP_RANGE добавлена защита от ловли падающих ножей: обязателен пробой экстремума
     предыдущей свечи (c_sig.close > prev_bar.high для Buy / c_sig.close < prev_bar.low для Sell)
     и отсутствие активного обвала/пампа по 15m EMA20/EMA50.
2. live_server.py:
   - Полный запрет входов по стратегиям со статусом QUARANTINE в обоих контурах (Booster и Core),
     чтобы убыточные стратегии с PF < 0.75 не открывали сделки даже уменьшенным лотом 0.35x.
3. release-manifest.json + полный прогон всех юнит-тестов.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    # -------------------------------------------------------------------------
    # 1. regime_s1_s3.py: er20 >= 0.22, 12-барный бокс S1 и защита от ножей в S3
    # -------------------------------------------------------------------------
    reg_p = ROOT / "aitrade/strategies/regime_s1_s3.py"
    reg_t = reg_p.read_text(encoding="utf-8")

    # 1a. Порог 1h-режима
    reg_t = reg_t.replace(
        "    if er20 >= 0.30 and sep >= 0.25:\n"
        "        return \"TREND_UP\" if ema20 > ema50 else \"TREND_DOWN\"\n"
        "    if er20 <= 0.28 and sep <= 0.85:\n"
        "        return \"RANGE\"",
        "    if er20 >= 0.22 and sep >= 0.25:\n"
        "        return \"TREND_UP\" if ema20 > ema50 else \"TREND_DOWN\"\n"
        "    if er20 < 0.22 and sep <= 0.85:\n"
        "        return \"RANGE\"",
    )
    reg_t = reg_t.replace(
        "    if er20 >= 0.30 and sep >= 0.25:\n"
        "        return \"TREND_UP\" if ema20 > ema50 else \"TREND_DOWN\"\n"
        "    if er20 <= 0.24 and sep <= 0.65:\n"
        "        return \"RANGE\"",
        "    if er20 >= 0.22 and sep >= 0.25:\n"
        "        return \"TREND_UP\" if ema20 > ema50 else \"TREND_DOWN\"\n"
        "    if er20 < 0.22 and sep <= 0.85:\n"
        "        return \"RANGE\"",
    )

    # 1b. 12-барный бокс для S1 и зона ретеста 0.25 * ATR15
    reg_t = reg_t.replace(
        "        lookback = closed_15m[-17:-1]\n"
        "        if len(lookback) < 16:",
        "        lookback = closed_15m[-13:-1]\n"
        "        if len(lookback) < 12:",
    )
    reg_t = reg_t.replace("retest_px = box_high + 0.1 * atr15", "retest_px = box_high + 0.25 * atr15")
    reg_t = reg_t.replace("retest_px = box_low - 0.1 * atr15", "retest_px = box_low - 0.25 * atr15")

    # 1c. Защита от ловли падающих ножей в S3_VWAP_RANGE
    old_s3_block = (
        "    if c_sig.low <= vwap_b.lower_2sd and c_sig.close > c_sig.open and entry_px < vwap_b.vwap:\n"
        "        side = \"Buy\"\n"
        "        sl_px = min(entry_px * (1.0 - MIN_STOP_FRAC), c_sig.low - 0.6 * atr15)\n"
        "    elif c_sig.high >= vwap_b.upper_2sd and c_sig.close < c_sig.open and entry_px > vwap_b.vwap:\n"
        "        side = \"Sell\"\n"
        "        sl_px = max(entry_px * (1.0 + MIN_STOP_FRAC), c_sig.high + 0.6 * atr15)"
    )
    new_s3_block = (
        "    prev_bar = closed_15m[-2]\n"
        "    closes_15m = [c.close for c in closed_15m[-60:]]\n"
        "    ema20_15m = calc_ema(closes_15m, 20)[-1]\n"
        "    ema50_15m = calc_ema(closes_15m, min(50, len(closes_15m)))[-1]\n"
        "    no_waterfall = not (ema20_15m < ema50_15m * 0.997)\n"
        "    no_meltup = not (ema20_15m > ema50_15m * 1.003)\n"
        "    if c_sig.low <= vwap_b.lower_2sd and c_sig.close > c_sig.open and c_sig.close > prev_bar.high and no_waterfall and entry_px < vwap_b.vwap:\n"
        "        side = \"Buy\"\n"
        "        sl_px = min(entry_px * (1.0 - MIN_STOP_FRAC), c_sig.low - 0.6 * atr15)\n"
        "    elif c_sig.high >= vwap_b.upper_2sd and c_sig.close < c_sig.open and c_sig.close < prev_bar.low and no_meltup and entry_px > vwap_b.vwap:\n"
        "        side = \"Sell\"\n"
        "        sl_px = max(entry_px * (1.0 + MIN_STOP_FRAC), c_sig.high + 0.6 * atr15)"
    )
    if old_s3_block in reg_t:
        reg_t = reg_t.replace(old_s3_block, new_s3_block, 1)
        print("[OK] regime_s1_s3.py: обновлены S1 (er20>=0.22, 12 баров) и защита от ножей S3")
    else:
        print("[SKIP/CHECK] regime_s1_s3.py: блок S3 уже обновлён или отличается")

    reg_p.write_text(reg_t, encoding="utf-8")

    # -------------------------------------------------------------------------
    # 2. live_server.py: полный запрет входа в стратегии со статусом QUARANTINE
    # -------------------------------------------------------------------------
    ls_p = ROOT / "aitrade/dashboard/live_server.py"
    ls_t = ls_p.read_text(encoding="utf-8")

    # Сохраняем cal_status на сигналах Booster и Core
    ls_t = ls_t.replace(
        's["cal_weight"] = mod["weight"]\n                s["cal_badge"] = mod["badge"]',
        's["cal_weight"] = mod["weight"]\n                s["cal_status"] = mod["status"]\n                s["cal_badge"] = mod["badge"]',
    )
    ls_t = ls_t.replace(
        'sig["cal_weight"] = mod["weight"]\n                    sig["cal_badge"] = mod["badge"]',
        'sig["cal_weight"] = mod["weight"]\n                    sig["cal_status"] = mod["status"]\n                    sig["cal_badge"] = mod["badge"]',
    )

    # Жёсткий пропуск QUARANTINE в Booster
    ls_t = ls_t.replace(
        'a_plus_list = [s for s in squeeze_list if s.get("grade") == "A+"]',
        'a_plus_list = [s for s in squeeze_list if s.get("grade") == "A+" and s.get("cal_status") != "QUARANTINE"]',
    )

    # Жёсткий пропуск QUARANTINE в Core
    ls_t = ls_t.replace(
        'and float(s.get("ml_win_probability", 0) or 0) >=',
        'and s.get("cal_status") != "QUARANTINE" and float(s.get("ml_win_probability", 0) or 0) >=',
    )

    ls_p.write_text(ls_t, encoding="utf-8")
    print("[OK] live_server.py: добавлена полная блокировка стратегий в статусе QUARANTINE")

    # -------------------------------------------------------------------------
    # 3. Очистка мусора ._* и обновление release-manifest.json
    # -------------------------------------------------------------------------
    for junk in ROOT.rglob("._*"):
        junk.unlink(missing_ok=True)

    mp = ROOT / "release-manifest.json"
    if mp.exists():
        m = json.loads(mp.read_text(encoding="utf-8"))
        for rel in list(m.get("files", {}).keys()):
            fp = ROOT / rel
            if fp.exists() and fp.is_file():
                m["files"][rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[OK] release-manifest.json обновлён")

    res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=str(ROOT))
    sys.exit(res.returncode)


if __name__ == "__main__":
    main()
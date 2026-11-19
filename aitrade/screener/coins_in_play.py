"""
4-ступенчатый пре-фильтр «Монеты в игре» (Coins in Play Screener) из раздела 2 документа:
1. Фильтр относительного объема (RVOL): отсекает неактивные рынки (RVOL < 0.6), выделяет активные (RVOL ≥ 1.2–1.5+).
2. Фильтр нормализованной волатильности (NATR %) и защита от хаотичного расширения ATR(14)/ATR_baseline(20) ≤ 1.5x.
3. Фильтр торгового трения и спреда: идеальный спред < 0.10%, пограничный 0.10%–0.30%, жесткое отсечение ≥ 0.30%.
4. Триада согласования таймфреймов (1H Bias по вееру EMA 8/21/50 + ADX, 15m/5m рабочая структура относительно VWAP)
   и синхронизация с глобальными торговыми сессиями UTC (Asia, Europe, US Overlap).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from aitrade.exchange.bybit_v5 import Candle, OrderBookSnapshot
from aitrade.features.indicators import (
    calc_adx,
    calc_natr_and_ratio,
    calc_rvol,
    calc_session_vwap_bands,
    determine_mtf_bias,
)


@dataclass
class ScreenerResult:
    symbol: str
    passed: bool
    in_play_score: float  # 0..100
    rvol: float
    natr_pct: float
    atr_expansion_ratio: float
    spread_pct: float
    turnover_24h_usd: float
    funding_rate: float
    mtf_bias: str  # "BULLISH_TREND" | "BEARISH_TREND" | "RANGE" | "TRANSITION"
    adx_1h: float
    active_session: str  # "ASIA" | "EUROPE" | "US_OVERLAP"
    recommended_playbooks: List[str]
    reject_reason: Optional[str] = None
    last_price: float = 0.0


def detect_active_session_utc(timestamp_ms: Optional[int] = None) -> str:
    """Определяет текущую глобальную торговую сессию по UTC."""
    if timestamp_ms is not None:
        dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=datetime.timezone.utc)
    else:
        dt = datetime.datetime.now(tz=datetime.timezone.utc)
    hour = dt.hour
    if 8 <= hour < 13:
        return "EUROPE"
    if 13 <= hour < 21:
        return "US_OVERLAP"
    return "ASIA"


class CoinsInPlayScreener:
    """Четырехступенчатый фильтр отбора ликвидных активов Bybit для интрадей-торговли."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        scr_cfg = settings.get("screener", {})
        self.min_rvol = float(scr_cfg.get("min_rvol", 1.2))
        self.min_rvol_active = float(scr_cfg.get("min_rvol_active_market", 0.6))
        self.max_spread_pct = float(scr_cfg.get("max_spread_pct", 0.10))
        self.borderline_spread_pct = float(scr_cfg.get("borderline_spread_pct", 0.30))
        self.max_atr_expansion = float(scr_cfg.get("max_atr_expansion_ratio", 1.5))
        self.min_turnover = float(scr_cfg.get("min_24h_turnover_usd", 15_000_000.0))

    def evaluate_coin(
        self,
        symbol: str,
        ticker_info: Dict[str, Any],
        candles_15m: List[Candle],
        candles_1h: List[Candle],
        orderbook: OrderBookSnapshot,
    ) -> ScreenerResult:
        rvol = calc_rvol(candles_15m, lookback=20)
        _, natr_pct, atr_exp_ratio = calc_natr_and_ratio(candles_15m)
        spread_pct = orderbook.spread_pct
        turnover_24h = float(ticker_info.get("turnover24h", 0.0) or 0.0)
        funding_rate = float(ticker_info.get("fundingRate", 0.0) or 0.0)

        mtf_bias, _, _, _ = determine_mtf_bias(candles_1h)
        adx_1h, _, _ = calc_adx(candles_1h, 14)
        session = detect_active_session_utc(candles_15m[-1].timestamp if candles_15m else None)
        vwap_bands = calc_session_vwap_bands(candles_15m)

        reject_reason: Optional[str] = None
        if spread_pct >= self.borderline_spread_pct:
            reject_reason = f"Спред {spread_pct:.3f}% >= порога {self.borderline_spread_pct:.2f}%"
        elif rvol < self.min_rvol_active:
            reject_reason = f"Рынок неактивен: RVOL {rvol:.2f} < {self.min_rvol_active:.2f}"
        elif turnover_24h > 0 and turnover_24h < self.min_turnover:
            reject_reason = f"Низкий 24ч оборот (${turnover_24h / 1e6:.1f}M < ${self.min_turnover / 1e6:.1f}M)"

        # Определяем рекомендованные архетипы стратегий с учетом режима ADX и сессии UTC:
        playbooks: List[str] = []
        if mtf_bias == "RANGE" or adx_1h < 25.0:
            # В балансе и при низком ADX работают снайперские возвраты к среднему (Winrate 65%-75%) и свипы
            playbooks.extend(["VWAP_KELTNER_MEAN_REVERSION", "LIQUIDITY_SWEEP_FVG", "OI_CVD_SQUEEZE_EXHAUSTION"])
        elif mtf_bias in ("BULLISH_TREND", "BEARISH_TREND"):
            if atr_exp_ratio <= self.max_atr_expansion:
                playbooks.append("VWAP_TREND_PULLBACK")
            playbooks.extend(["LIQUIDITY_SWEEP_FVG", "OI_CVD_SQUEEZE_EXHAUSTION"])
            if session in ("EUROPE", "US_OVERLAP") and rvol >= self.min_rvol:
                playbooks.append("SESSION_ORB_BREAKOUT")
            if symbol != "BTCUSDT" and session != "US_OVERLAP":
                playbooks.append("LEAD_LAG_DENSITY")
        else:
            # TRANSITION — разрешаем только подтвержденные ложные выносы и истощение сквиза по OI/CVD
            playbooks.extend(["LIQUIDITY_SWEEP_FVG", "OI_CVD_SQUEEZE_EXHAUSTION"])

        # Интегральный балл «Монеты в игре» (0..100)
        rvol_score = min(35.0, rvol * 14.0)
        natr_score = min(25.0, natr_pct * 15.0)
        spread_score = max(0.0, 20.0 * (1.0 - spread_pct / max(self.max_spread_pct, 0.01)))
        funding_bonus = 10.0 if abs(funding_rate) >= 0.0003 else 4.0
        vwap_stretch = (
            10.0
            if (candles_15m and abs(candles_15m[-1].close - vwap_bands.vwap) >= vwap_bands.std * 1.5)
            else 5.0
        )
        in_play_score = round(min(100.0, rvol_score + natr_score + spread_score + funding_bonus + vwap_stretch), 1)

        last_px = float(candles_15m[-1].close) if candles_15m else float(ticker_info.get("lastPrice", 0.0) or 0.0)
        return ScreenerResult(
            symbol=symbol,
            passed=(reject_reason is None),
            in_play_score=in_play_score,
            rvol=round(rvol, 2),
            natr_pct=round(natr_pct, 3),
            atr_expansion_ratio=round(atr_exp_ratio, 2),
            spread_pct=round(spread_pct, 4),
            turnover_24h_usd=round(turnover_24h, 2),
            funding_rate=funding_rate,
            mtf_bias=mtf_bias,
            adx_1h=round(adx_1h, 1),
            active_session=session,
            recommended_playbooks=playbooks,
            reject_reason=reject_reason,
            last_price=round(last_px, 6),
        )

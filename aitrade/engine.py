"""
Главный оркестратор ИИ-комбайна (AITradeCombineEngine, релиз 2026):
1. Подключение к Bybit V5 (реальные свечи 5m/15m/1h/4h, стакан Level-2, OI, Funding, Recent Trades).
2. Четырёхступенчатый пре-скринер «Монеты в игре» (Coins in Play: RVOL, NATR, Spread, MTF Bias).
3. Генерация сигналов обоих изолированных контуров:
   - Разгон: B1_SQUEEZE_BREAKOUT (Volatility Squeeze + Supertrend) и B2/S1_TREND_RETEST (PostOnly ретест).
   - Автотрейдинг (в ликвидные часы 07:00–20:00 UTC):
     * Рукав A: S3_VWAP_RANGE (PostOnly Limit, R:R >= 1.15, SL >= 0.80%).
     * Рукав B: AUTOTRADE_MOMENTUM_FUNDING (кросс-секционный Моментум + Фандинг).
4. Валидация через реальный Meta-Labeler (Triple Barrier + Purged K-Fold + калибровка),
   RiskEngine и DebateCouncil.
"""

from __future__ import annotations

import datetime
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from aitrade.agents.debate_council import MultiAgentDebateCouncil
from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import BybitV5Client, Candle
from aitrade.exchange.coinalyze import CoinalyzeClient
from aitrade.execution.demo_trader import BybitDemoExecutor
from aitrade.features.indicators import calc_session_vwap_bands, calc_volume_profile
from aitrade.features.microstructure import analyze_microstructure
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.risk.risk_engine import RiskEngine
from aitrade.screener.coins_in_play import CoinsInPlayScreener, ScreenerResult
from aitrade.strategies.base import MarketContext, TradeSignal
from aitrade.strategies.booster_breakout import generate_booster_breakout_signal, is_weekend_chop_utc
from aitrade.strategies.donchian_intraday import generate_donchian_intraday_signal
from aitrade.strategies.donchian_tsmom import generate_donchian_tsmom_signal
from aitrade.strategies.ema_pullback_trend import generate_ema_pullback_signal
from aitrade.strategies.funding_squeeze_momentum import generate_funding_squeeze_signal
from aitrade.strategies.keltner_trend_4h import generate_keltner_trend_4h_signal
from aitrade.strategies.mtf_combo_breakout import generate_mtf_combo_signal
from aitrade.strategies.oi_trend_expansion import generate_oi_trend_expansion_signal
from aitrade.strategies.orb_session_breakout import generate_orb_session_signal, is_in_orb_session_window
from aitrade.strategies.rs_btc_breakout import generate_rs_btc_signal
from aitrade.strategies.vwap_momentum_impulse import generate_vwap_momentum_signal
from aitrade.strategies.funding_carry import FundingCarryEngine
from aitrade.strategies.lead_lag_density import evaluate_lead_lag_density_strategy
from aitrade.strategies.liquidity_sweep_fvg import evaluate_liquidity_sweep_fvg_strategy
from aitrade.strategies.momentum_funding import generate_momentum_funding_signals
from aitrade.strategies.oi_cvd_squeeze import evaluate_oi_cvd_squeeze_strategy
from aitrade.strategies.orb_breakout import evaluate_orb_breakout_strategy
from aitrade.strategies.regime_s1_s3 import generate_s1_s3_signal
from aitrade.strategies.vwap_keltner_mr import evaluate_vwap_keltner_strategy


def is_in_autotrade_liquid_window(
    timestamp_ms: Optional[int], settings: Dict[str, Any]
) -> bool:
    """Проверяет, попадает ли UTC-время в ликвидное окно Автотрейдинга (по умолчанию 07:00–20:00 UTC)."""
    win = (settings.get("sessions_utc") or {}).get("autotrade_liquid_window")
    if not isinstance(win, dict):
        return True
    start_h = int(win.get("start_hour", 7))
    end_h = int(win.get("end_hour", 20))
    if timestamp_ms is not None and int(timestamp_ms) > 0:
        dt = datetime.datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=datetime.timezone.utc)
    else:
        dt = datetime.datetime.now(tz=datetime.timezone.utc)
    return start_h <= dt.hour < end_h


class AITradeCombineEngine:
    """Центральное ядро ИИ-комбайна AItrade для Bybit V5."""

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or load_settings()
        self.client = BybitV5Client(
            self.settings,
            allow_offline_fallback=bool(self.settings.get("offline_simulation", False)),
        )
        self.coinalyze = CoinalyzeClient(self.settings.get("coinalyze_api_key", ""))
        self.screener = CoinsInPlayScreener(self.settings)
        self.ml_labeler = WalkForwardMetaLabeler(self.settings)
        self.debate_council = MultiAgentDebateCouncil(self.settings)
        self.risk_engine = RiskEngine(self.settings)
        self.funding_carry = FundingCarryEngine()
        self.demo_executor = BybitDemoExecutor(self.client, self.settings)

    def build_market_context(
        self,
        symbol: str,
        ticker_map: Dict[str, Dict[str, Any]],
        btc_candles_15m: List[Any],
    ) -> MarketContext:
        ticker = ticker_map.get(symbol, {"symbol": symbol})
        is_legacy = self.settings.get("strategy_mode", "s1_s3") == "legacy"
        c15 = self.client.get_klines(symbol, interval="15", limit=600)
        c1h = self.client.get_klines(symbol, interval="60", limit=220)
        c4h = self.client.get_klines(symbol, interval="240", limit=120)
        c5m = self.client.get_klines(symbol, interval="5", limit=60) if is_legacy else []
        orderbook = self.client.get_orderbook(symbol, limit=50)
        ob_hist = self.client.get_orderbook_history(symbol) if is_legacy else []
        oi_hist = self.client.get_open_interest_history(symbol, interval_time="15min", limit=40)
        recent_trades = self.client.get_recent_trades(symbol, limit=150) if is_legacy else []

        c15_closed = c15[:-1] if len(c15) > 1 else c15
        c1h_closed = c1h[:-1] if len(c1h) > 1 else c1h
        vwap_bands = calc_session_vwap_bands(c15_closed)
        vol_profile = calc_volume_profile(c15_closed)
        micro = analyze_microstructure(c15_closed, orderbook, ob_hist, recent_trades)
        scr_res = self.screener.evaluate_coin(symbol, ticker, c15_closed, c1h_closed, orderbook)

        return MarketContext(
            symbol=symbol,
            ticker=ticker,
            candles_5m=c5m[:-1] if len(c5m) > 1 else c5m,
            candles_15m=c15_closed,
            candles_1h=c1h_closed,
            candles_4h=c4h[:-1] if len(c4h) > 1 else c4h,
            btc_candles_15m=btc_candles_15m[:-1] if len(btc_candles_15m) > 1 else btc_candles_15m,
            orderbook=orderbook,
            oi_history=oi_hist,
            vwap_bands=vwap_bands,
            volume_profile=vol_profile,
            micro=micro,
            screener=scr_res,
        )

    def scan_market(
        self,
        symbols: Optional[List[str]] = None,
        auto_execute_demo: bool = False,
        max_signals: int = 8,
    ) -> Dict[str, Any]:
        uni = self.settings.get("universe", {})
        core_pairs = symbols or uni.get("core_pairs", [])
        autotrade_set = set(uni.get("autotrade_pairs") or core_pairs)
        booster_set = set(uni.get("booster_pairs") or core_pairs)

        all_tickers = self.client.get_tickers()
        ticker_map = {t["symbol"]: t for t in all_tickers if "symbol" in t}
        btc_15m = self.client.get_klines("BTCUSDT", interval="15", limit=120)
        equity_usd = min(
            self.demo_executor.get_account_equity_usdt(),
            float(self.settings.get("risk", {}).get("allocated_equity_usdt", 1000.0)),
        )

        screener_results: List[ScreenerResult] = []
        raw_candidates: List[TradeSignal] = []
        approved_signals: List[TradeSignal] = []
        rejected_signals: List[TradeSignal] = []

        max_workers = min(12, max(1, len(core_pairs)))
        context_errors: List[Dict[str, str]] = []

        def build_one(symbol: str):
            try:
                ctx = self.build_market_context(symbol, ticker_map, btc_15m)
                if len(ctx.candles_15m) < 45 or not ctx.orderbook.bids or not ctx.orderbook.asks:
                    raise ValueError("Недостаточно рыночных данных")
                return symbol, ctx
            except Exception as exc:
                context_errors.append({"symbol": symbol, "error": str(exc)[:160]})
                return symbol, None

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            contexts = list(pool.map(build_one, core_pairs))

        ctx_by_sym: Dict[str, MarketContext] = {}
        h1_autotrade: Dict[str, List[Candle]] = {}
        fr_autotrade: Dict[str, float] = {}
        last_ts_ms: Optional[int] = None

        for sym, ctx in contexts:
            if ctx is None:
                continue
            ctx_by_sym[sym] = ctx
            screener_results.append(ctx.screener)
            if ctx.candles_15m:
                last_ts_ms = max(last_ts_ms or 0, int(ctx.candles_15m[-1].timestamp))
            if sym in autotrade_set and len(ctx.candles_1h) >= 30:
                h1_autotrade[sym] = ctx.candles_1h
                fr_autotrade[sym] = float(ctx.screener.funding_rate or 0.0)

        in_liquid_hours = is_in_autotrade_liquid_window(last_ts_ms, self.settings)
        in_weekend_chop = is_weekend_chop_utc(last_ts_ms) if last_ts_ms else False

        diag: Dict[str, Dict[str, Any]] = {
            sid: {
                "eligible_window": False,
                "pairs_checked": 0,
                "raw_candidates": 0,
                "blocked_wfa_ml": 0,
                "approved": 0,
                "idle_reason": "",
            }
            for sid in (
                "T1_DONCHIAN_TSMOM",
                "B1_SQUEEZE_BREAKOUT",
                "B2_DONCHIAN_INTRADAY",
                "B3_VWAP_MOMENTUM_IMPULSE",
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
        }
        t1_out_of_30m_window = 0
        t1_wide_spread_or_short = 0
        booster_in_play_pairs = 0

        def evaluate_and_bucket(sig: TradeSignal, ctx: MarketContext) -> None:
            if not sig.signal_id:
                sig.signal_id = "sig-" + uuid.uuid4().hex[:16]
            if not sig.signal_created_ms:
                sig.signal_created_ms = int(time.time() * 1000)
            raw_candidates.append(sig)
            sig = self.ml_labeler.evaluate_signal(sig, ctx)
            sig = self.risk_engine.size_and_validate_signal(sig, ctx.orderbook, equity_usd)
            sig = self.debate_council.conduct_debate(sig, ctx)
            d_row = diag.get(sig.strategy_id)
            if d_row is not None:
                d_row["raw_candidates"] += 1
            if sig.debate_verdict == "APPROVED" and sig.passed_ml_filter:
                approved_signals.append(sig)
                if d_row is not None:
                    d_row["approved"] += 1
            else:
                rejected_signals.append(sig)
                if d_row is not None:
                    d_row["blocked_wfa_ml"] += 1
                    cal_st = str((sig.features or {}).get("ml_calibration_status") or "")
                    if cal_st == "WFA_VALIDATION_UNAVAILABLE":
                        d_row["idle_reason"] = "Fail-closed: нет data/wfa_validation.json"
                    elif cal_st == "WARMING_UP_BLOCKED_BY_WFA":
                        d_row["idle_reason"] = "Заблокирован WFA Gatekeeper (wfe_passed=false)"
                    elif not sig.passed_ml_filter:
                        d_row["idle_reason"] = f"Отклонён ML/Cost-фильтром ({cal_st or 'ML'})"
                    else:
                        d_row["idle_reason"] = f"Отклонён DebateCouncil ({sig.debate_verdict})"

        for sym, ctx in ctx_by_sym.items():
            sym_signals: List[TradeSignal] = []
            if self.settings.get("strategy_mode", "s1_s3") != "legacy":
                # 0) T1_DONCHIAN_TSMOM (Автотрейдинг Ядро 4h, 24/7, вход только в первые 30 мин после закрытия 4h бара)
                if sym in autotrade_set:
                    if len(ctx.candles_4h) >= 85 and ctx.screener.spread_pct <= 0.15:
                        last_4h_close_ms = int(ctx.candles_4h[-1].timestamp) + 14_400_000
                        last_15m_close_ms = (int(ctx.candles_15m[-1].timestamp) + 900_000) if ctx.candles_15m else last_4h_close_ms
                        bar_age_ms = last_15m_close_ms - last_4h_close_ms
                        if -900_000 <= bar_age_ms <= 30 * 60_000:
                            diag["T1_DONCHIAN_TSMOM"]["eligible_window"] = True
                            diag["T1_DONCHIAN_TSMOM"]["pairs_checked"] += 1
                            ref_live_px = ctx.orderbook.mid_price if ctx.orderbook.mid_price > 0 else ctx.candles_4h[-1].close
                            t1_sig = generate_donchian_tsmom_signal(
                                sym,
                                ctx.candles_4h,
                                reference_open_px=ref_live_px,
                                funding_rate=float(ctx.screener.funding_rate or 0.0),
                                horizons=(20, 40, 80),
                                target_ann_vol=0.25,
                                max_hold_bars=60,
                                tp_rr=8.0,
                            )
                            if t1_sig is not None:
                                sym_signals.append(t1_sig)
                        else:
                            t1_out_of_30m_window += 1
                    else:
                        t1_wide_spread_or_short += 1

                # 0b) 1h/4h Demo Lab стратегии (B2 Donchian 1h, T2 RS vs BTC, C2 MTF Combo) — по фильтру спреда на всех 20 парах
                if ctx.screener.spread_pct <= 0.15 and len(ctx.candles_1h) >= 35:
                    ref_px_live = ctx.orderbook.mid_price if ctx.orderbook.mid_price > 0 else ctx.candles_15m[-1].close
                    diag["B2_DONCHIAN_INTRADAY"]["eligible_window"] = True
                    diag["B2_DONCHIAN_INTRADAY"]["pairs_checked"] += 1
                    b2_sig = generate_donchian_intraday_signal(
                        sym, ctx.candles_1h, bars_4h=ctx.candles_4h,
                        reference_open_px=ref_px_live, funding_rate=float(ctx.screener.funding_rate or 0.0),
                    )
                    if b2_sig is not None:
                        sym_signals.append(b2_sig)

                    if sym != "BTCUSDT" and len(ctx.btc_candles_15m) >= 96:
                        diag["T2_RS_BTC_BREAKOUT"]["eligible_window"] = True
                        diag["T2_RS_BTC_BREAKOUT"]["pairs_checked"] += 1
                        t2_sig = generate_rs_btc_signal(
                            sym, ctx.candles_1h, ctx.btc_candles_15m,
                            funding_rate=float(ctx.screener.funding_rate or 0.0), reference_open_px=ref_px_live,
                        )
                        if t2_sig is not None:
                            sym_signals.append(t2_sig)

                    if len(ctx.candles_4h) >= 52 and len(ctx.btc_candles_15m) >= 96:
                        diag["C2_MTF_COMBO_BREAKOUT"]["eligible_window"] = True
                        diag["C2_MTF_COMBO_BREAKOUT"]["pairs_checked"] += 1
                        c2_sig = generate_mtf_combo_signal(
                            sym, ctx.candles_15m, ctx.candles_1h, ctx.candles_4h, ctx.btc_candles_15m,
                            funding_rate=float(ctx.screener.funding_rate or 0.0), reference_open_px=ref_px_live,
                        )
                        if c2_sig is not None:
                            sym_signals.append(c2_sig)

                    diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["eligible_window"] = True
                    diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["pairs_checked"] += 1
                    b7_sig = generate_funding_squeeze_signal(
                        sym, ctx.candles_1h, funding_rate=float(ctx.screener.funding_rate or 0.0),
                        reference_open_px=ref_px_live,
                    )
                    if b7_sig is not None:
                        sym_signals.append(b7_sig)

                    if len(ctx.candles_4h) >= 35:
                        diag["T3_KELTNER_TREND_4H"]["eligible_window"] = True
                        diag["T3_KELTNER_TREND_4H"]["pairs_checked"] += 1
                        t3_sig = generate_keltner_trend_4h_signal(
                            sym, ctx.candles_4h, funding_rate=float(ctx.screener.funding_rate or 0.0),
                            reference_open_px=ref_px_live,
                        )
                        if t3_sig is not None:
                            sym_signals.append(t3_sig)

                    if len(ctx.candles_4h) >= 52 and len(ctx.candles_15m) >= 40:
                        diag["B6_EMA_PULLBACK_TREND"]["eligible_window"] = True
                        diag["B6_EMA_PULLBACK_TREND"]["pairs_checked"] += 1
                        b6_sig = generate_ema_pullback_signal(
                            sym, ctx.candles_15m, ctx.candles_1h, ctx.candles_4h,
                            funding_rate=float(ctx.screener.funding_rate or 0.0), reference_open_px=ref_px_live,
                        )
                        if b6_sig is not None:
                            sym_signals.append(b6_sig)

                # 0c) 15m Demo Lab стратегии Разгона (B3/B4/B5 имеют собственные фильтры RVOL/OI, не запираем за жёстким CoinsInPlay)
                if ctx.screener.spread_pct <= 0.15 and len(ctx.candles_15m) >= 40:
                    ref_px_15 = ctx.orderbook.mid_price if ctx.orderbook.mid_price > 0 else ctx.candles_15m[-1].close
                    diag["B3_VWAP_MOMENTUM_IMPULSE"]["eligible_window"] = True
                    diag["B3_VWAP_MOMENTUM_IMPULSE"]["pairs_checked"] += 1
                    b3_sig = generate_vwap_momentum_signal(
                        sym, ctx.candles_15m, bars_1h=ctx.candles_1h,
                        rvol=float(ctx.screener.rvol or 1.0), funding_rate=float(ctx.screener.funding_rate or 0.0),
                        reference_open_px=ref_px_15,
                    )
                    if b3_sig is not None:
                        sym_signals.append(b3_sig)

                    if is_in_orb_session_window(int(ctx.candles_15m[-1].timestamp)):
                        diag["B4_ORB_SESSION_BREAKOUT"]["eligible_window"] = True
                        diag["B4_ORB_SESSION_BREAKOUT"]["pairs_checked"] += 1
                        b4_sig = generate_orb_session_signal(
                            sym, ctx.candles_15m,
                            rvol=float(ctx.screener.rvol or 1.0), funding_rate=float(ctx.screener.funding_rate or 0.0),
                            reference_open_px=ref_px_15,
                        )
                        if b4_sig is not None:
                            sym_signals.append(b4_sig)

                    diag["B5_OI_TREND_EXPANSION"]["eligible_window"] = True
                    diag["B5_OI_TREND_EXPANSION"]["pairs_checked"] += 1
                    b5_sig = generate_oi_trend_expansion_signal(
                        sym, ctx.candles_15m, ctx.oi_history,
                        funding_rate=float(ctx.screener.funding_rate or 0.0), reference_open_px=ref_px_15,
                    )
                    if b5_sig is not None:
                        sym_signals.append(b5_sig)

            if ctx.screener.passed:
                if self.settings.get("strategy_mode", "s1_s3") == "legacy":
                    sym_signals.extend(evaluate_vwap_keltner_strategy(ctx))
                    sym_signals.extend(evaluate_liquidity_sweep_fvg_strategy(ctx))
                    sym_signals.extend(evaluate_oi_cvd_squeeze_strategy(ctx))
                    sym_signals.extend(evaluate_orb_breakout_strategy(ctx))
                    if sym != "BTCUSDT":
                        sym_signals.extend(evaluate_lead_lag_density_strategy(ctx))
                else:
                    if sym in booster_set:
                        booster_in_play_pairs += 1
                        diag["S1_TREND_RETEST"]["eligible_window"] = True
                        diag["S1_TREND_RETEST"]["pairs_checked"] += 1
                        if not in_weekend_chop:
                            diag["B1_SQUEEZE_BREAKOUT"]["eligible_window"] = True
                            diag["B1_SQUEEZE_BREAKOUT"]["pairs_checked"] += 1
                    if sym in autotrade_set and in_liquid_hours:
                        diag["S3_VWAP_RANGE"]["eligible_window"] = True
                        diag["S3_VWAP_RANGE"]["pairs_checked"] += 1

                    best_bid = ctx.orderbook.best_bid
                    best_ask = ctx.orderbook.best_ask
                    # 1) S1_TREND_RETEST и S3_VWAP_RANGE
                    s1_s3_sig = generate_s1_s3_signal(
                        sym,
                        ctx.candles_15m,
                        bars_1h=ctx.candles_1h,
                        min_rr_s3=1.15,
                        obi_smoothed=ctx.micro.smoothed_obi,
                        best_bid=best_bid,
                        best_ask=best_ask,
                    )
                    if s1_s3_sig is not None:
                        if s1_s3_sig.strategy_id == "S3_VWAP_RANGE":
                            if sym in autotrade_set and in_liquid_hours:
                                sym_signals.append(s1_s3_sig)
                        elif sym in booster_set:
                            sym_signals.append(s1_s3_sig)

                    # 2) B1_SQUEEZE_BREAKOUT (Разгон Ядро B1, 6 пар, с фильтром выходных NoWeekendChop)
                    if sym in booster_set:
                        ref_px_b1 = ctx.orderbook.mid_price if ctx.orderbook.mid_price > 0 else ctx.candles_15m[-1].close
                        b1_sig = generate_booster_breakout_signal(
                            sym,
                            ctx.candles_15m,
                            reference_open_px=ref_px_b1,
                            bars_1h=ctx.candles_1h,
                            funding_rate=ctx.screener.funding_rate,
                            obi_smoothed=ctx.micro.smoothed_obi,
                            best_bid=best_bid,
                            best_ask=best_ask,
                            exclude_weekend_chop=True,
                        )
                        if b1_sig is not None:
                            sym_signals.append(b1_sig)

            diag["C3_ENSEMBLE_VOTE_2OF3"]["eligible_window"] = True
            diag["C3_ENSEMBLE_VOTE_2OF3"]["pairs_checked"] += 1
            for v_side in ("Buy", "Sell"):
                voters = [s for s in sym_signals if s.side == v_side and s.strategy_id != "C3_ENSEMBLE_VOTE_2OF3"]
                unique_voters = {s.strategy_id for s in voters}
                if len(unique_voters) >= 2:
                    ref_s = voters[0]
                    avg_sl_dist = sum(abs(s.entry_price - s.stop_loss) for s in voters) / len(voters)
                    c3_entry = ref_s.entry_price
                    c3_sl = c3_entry - avg_sl_dist if v_side == "Buy" else c3_entry + avg_sl_dist
                    c3_tp = c3_entry + avg_sl_dist * 3.5 if v_side == "Buy" else c3_entry - avg_sl_dist * 3.5
                    if c3_sl > 0 and c3_tp > 0:
                        sym_signals.append(
                            TradeSignal(
                                symbol=sym,
                                strategy_id="C3_ENSEMBLE_VOTE_2OF3",
                                strategy_name=f"Ensemble Vote ({len(unique_voters)}x)",
                                timeframe=ref_s.timeframe,
                                side=v_side,
                                order_type="Market",
                                entry_price=round(c3_entry, 6),
                                stop_loss=round(c3_sl, 6),
                                take_profit_1=round(c3_tp, 6),
                                take_profit_2=round(c3_tp, 6),
                                base_winrate_est=0.52,
                                rationale=["Согласованный сигнал " + str(len(unique_voters)) + " стратегий: " + ", ".join(sorted(unique_voters))],
                                invalidation_rule="Pure SL/TP (3.5R) или закрытие через 24ч",
                                features={"post_only": 0.0, "ensemble_votes": float(len(unique_voters))},
                            )
                        )

            for sig in sym_signals:
                evaluate_and_bucket(sig, ctx)

        # 3) Рукав B Автотрейдинга: кросс-секционный «Моментум + Фандинг» (только в ликвидные часы UTC)
        if in_liquid_hours and len(h1_autotrade) >= 2 and self.settings.get("strategy_mode", "s1_s3") != "legacy":
            diag["AUTOTRADE_MOMENTUM_FUNDING"]["eligible_window"] = True
            diag["AUTOTRADE_MOMENTUM_FUNDING"]["pairs_checked"] = len(h1_autotrade)
            mom_sigs = generate_momentum_funding_signals(h1_autotrade, fr_autotrade, top_n=1)
            for m_sig in mom_sigs:
                m_ctx = ctx_by_sym.get(m_sig.symbol)
                if m_ctx is not None:
                    evaluate_and_bucket(m_sig, m_ctx)

        # Заполняем явную причину простоя для каждой стратегии без одобренного сигнала
        if diag["T1_DONCHIAN_TSMOM"]["approved"] > 0:
            diag["T1_DONCHIAN_TSMOM"]["idle_reason"] = "Есть одобренный сигнал T1"
        elif not diag["T1_DONCHIAN_TSMOM"]["idle_reason"]:
            if diag["T1_DONCHIAN_TSMOM"]["eligible_window"]:
                diag["T1_DONCHIAN_TSMOM"]["idle_reason"] = f"Нет пробоя Donchian 20/40/80 на {diag['T1_DONCHIAN_TSMOM']['pairs_checked']} парах"
            elif t1_out_of_30m_window > 0:
                diag["T1_DONCHIAN_TSMOM"]["idle_reason"] = "Вне 30-мин окна закрытия 4h-бара (00/04/08/12/16/20 UTC)"
            elif t1_wide_spread_or_short > 0:
                diag["T1_DONCHIAN_TSMOM"]["idle_reason"] = "Мало 4h-баров (<85) или спред > 0.15%"
            else:
                diag["T1_DONCHIAN_TSMOM"]["idle_reason"] = "Нет пар Автотрейдинга в контексте"

        if diag["B1_SQUEEZE_BREAKOUT"]["approved"] > 0:
            diag["B1_SQUEEZE_BREAKOUT"]["idle_reason"] = "Есть одобренный сигнал B1"
        elif not diag["B1_SQUEEZE_BREAKOUT"]["idle_reason"]:
            if in_weekend_chop:
                diag["B1_SQUEEZE_BREAKOUT"]["idle_reason"] = "Окно выходных NoWeekendChop (Сб 18:00–Вс 22:00 UTC)"
            elif booster_in_play_pairs == 0:
                diag["B1_SQUEEZE_BREAKOUT"]["idle_reason"] = f"0 из {len(booster_set)} пар Разгона прошли CoinsInPlay"
            else:
                diag["B1_SQUEEZE_BREAKOUT"]["idle_reason"] = f"Нет BB/KC-сжатия и пробоя на {booster_in_play_pairs} активных парах"

        if diag["B2_DONCHIAN_INTRADAY"]["approved"] > 0:
            diag["B2_DONCHIAN_INTRADAY"]["idle_reason"] = "Есть одобренный сигнал B2 (1h Donchian)"
        elif not diag["B2_DONCHIAN_INTRADAY"]["idle_reason"]:
            p_chk = diag["B2_DONCHIAN_INTRADAY"]["pairs_checked"]
            diag["B2_DONCHIAN_INTRADAY"]["idle_reason"] = f"Нет пробоя 12h/24h Donchian по 4h-тренду на {p_chk} парах" if p_chk > 0 else "Нет пар в игре (CoinsInPlay)"

        if diag["B3_VWAP_MOMENTUM_IMPULSE"]["approved"] > 0:
            diag["B3_VWAP_MOMENTUM_IMPULSE"]["idle_reason"] = "Есть одобренный сигнал B3 (VWAP импульс)"
        elif not diag["B3_VWAP_MOMENTUM_IMPULSE"]["idle_reason"]:
            p_chk3 = diag["B3_VWAP_MOMENTUM_IMPULSE"]["pairs_checked"]
            diag["B3_VWAP_MOMENTUM_IMPULSE"]["idle_reason"] = f"Нет импульса от VWAP (Z=0.45..2.2, RVOL>=1.35) на {p_chk3} парах" if p_chk3 > 0 else "Нет пар в игре (CoinsInPlay)"

        if diag["B4_ORB_SESSION_BREAKOUT"]["approved"] > 0:
            diag["B4_ORB_SESSION_BREAKOUT"]["idle_reason"] = "Есть одобренный сигнал B4 (Session ORB)"
        elif not diag["B4_ORB_SESSION_BREAKOUT"]["idle_reason"]:
            p_chk4 = diag["B4_ORB_SESSION_BREAKOUT"]["pairs_checked"]
            if not diag["B4_ORB_SESSION_BREAKOUT"]["eligible_window"]:
                diag["B4_ORB_SESSION_BREAKOUT"]["idle_reason"] = "Вне окна ORB (ждёт 01:00–04:59 / 08:00–11:59 / 14:00–17:59 UTC)"
            elif p_chk4 > 0:
                diag["B4_ORB_SESSION_BREAKOUT"]["idle_reason"] = f"Нет свежего пробоя диапазона сессии (00/07/13 UTC) на {p_chk4} парах"
            else:
                diag["B4_ORB_SESSION_BREAKOUT"]["idle_reason"] = "Нет пар в игре (CoinsInPlay)"

        if diag["B5_OI_TREND_EXPANSION"]["approved"] > 0:
            diag["B5_OI_TREND_EXPANSION"]["idle_reason"] = "Есть одобренный сигнал B5 (OI-пробой)"
        elif not diag["B5_OI_TREND_EXPANSION"]["idle_reason"]:
            p_chk5 = diag["B5_OI_TREND_EXPANSION"]["pairs_checked"]
            diag["B5_OI_TREND_EXPANSION"]["idle_reason"] = f"Нет пробоя 20b с ростом OI (>=+1.2%) на {p_chk5} парах" if p_chk5 > 0 else "Нет пар в игре (CoinsInPlay)"

        if diag["T2_RS_BTC_BREAKOUT"]["approved"] > 0:
            diag["T2_RS_BTC_BREAKOUT"]["idle_reason"] = "Есть одобренный сигнал T2 (RS vs BTC)"
        elif not diag["T2_RS_BTC_BREAKOUT"]["idle_reason"]:
            p_t2 = diag["T2_RS_BTC_BREAKOUT"]["pairs_checked"]
            diag["T2_RS_BTC_BREAKOUT"]["idle_reason"] = f"Нет лидера/аутсайдера (RS>=2.2% к BTC + 12h пробой) на {p_t2} парах"

        if diag["C2_MTF_COMBO_BREAKOUT"]["approved"] > 0:
            diag["C2_MTF_COMBO_BREAKOUT"]["idle_reason"] = "Есть одобренный сигнал C2 (MTF Combo)"
        elif not diag["C2_MTF_COMBO_BREAKOUT"]["idle_reason"]:
            p_c2 = diag["C2_MTF_COMBO_BREAKOUT"]["pairs_checked"]
            diag["C2_MTF_COMBO_BREAKOUT"]["idle_reason"] = f"Нет совпадения 4 факторов (4h+RS+VWAP+8h) на {p_c2} парах"

        if diag["B6_EMA_PULLBACK_TREND"]["approved"] > 0:
            diag["B6_EMA_PULLBACK_TREND"]["idle_reason"] = "Есть одобренный сигнал B6 (EMA21 откат)"
        elif not diag["B6_EMA_PULLBACK_TREND"]["idle_reason"]:
            p_b6 = diag["B6_EMA_PULLBACK_TREND"]["pairs_checked"]
            diag["B6_EMA_PULLBACK_TREND"]["idle_reason"] = f"Нет отката к EMA21 в 4h-тренде на {p_b6} парах"

        if diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["approved"] > 0:
            diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["idle_reason"] = "Есть одобренный сигнал B7 (Сквиз фандинга)"
        elif not diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["idle_reason"]:
            p_b7 = diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["pairs_checked"]
            diag["B7_FUNDING_SQUEEZE_MOMENTUM"]["idle_reason"] = f"Нет экстремального фандинга + пробоя 4h на {p_b7} парах"

        if diag["T3_KELTNER_TREND_4H"]["approved"] > 0:
            diag["T3_KELTNER_TREND_4H"]["idle_reason"] = "Есть одобренный сигнал T3 (4h Keltner)"
        elif not diag["T3_KELTNER_TREND_4H"]["idle_reason"]:
            p_t3 = diag["T3_KELTNER_TREND_4H"]["pairs_checked"]
            diag["T3_KELTNER_TREND_4H"]["idle_reason"] = f"Нет свежего пробоя 4h канала Кельтнера на {p_t3} парах"

        if diag["C3_ENSEMBLE_VOTE_2OF3"]["approved"] > 0:
            diag["C3_ENSEMBLE_VOTE_2OF3"]["idle_reason"] = "Есть ансамблевый сигнал C3 (>=2 голосов)"
        elif not diag["C3_ENSEMBLE_VOTE_2OF3"]["idle_reason"]:
            diag["C3_ENSEMBLE_VOTE_2OF3"]["idle_reason"] = "Нет одновременного совпадения >=2 стратегий на одной монете"

        if diag["S1_TREND_RETEST"]["approved"] > 0:
            diag["S1_TREND_RETEST"]["idle_reason"] = "Есть одобренный сигнал S1"
        elif not diag["S1_TREND_RETEST"]["idle_reason"]:
            diag["S1_TREND_RETEST"]["idle_reason"] = "Нет ретеста тренда на активных парах" if booster_in_play_pairs > 0 else "Нет пар Разгона в игре"

        if diag["S3_VWAP_RANGE"]["approved"] > 0:
            diag["S3_VWAP_RANGE"]["idle_reason"] = "Есть одобренный сигнал S3"
        elif not diag["S3_VWAP_RANGE"]["idle_reason"]:
            diag["S3_VWAP_RANGE"]["idle_reason"] = "Вне ликвидного окна 07:00–20:00 UTC" if not in_liquid_hours else "Нет возврата к VWAP (и стратегия заблокирована WFA)"

        if diag["AUTOTRADE_MOMENTUM_FUNDING"]["approved"] > 0:
            diag["AUTOTRADE_MOMENTUM_FUNDING"]["idle_reason"] = "Есть одобренный сигнал Momentum+Funding"
        elif not diag["AUTOTRADE_MOMENTUM_FUNDING"]["idle_reason"]:
            diag["AUTOTRADE_MOMENTUM_FUNDING"]["idle_reason"] = "Вне ликвидного окна 07:00–20:00 UTC" if not in_liquid_hours else "Нет кросс-секционного сигнала (и стратегия заблокирована WFA)"

        tier_ranks = {
            "T1_DONCHIAN_TSMOM": 0,
            "B1_SQUEEZE_BREAKOUT": 1,
            "C3_ENSEMBLE_VOTE_2OF3": 2,
            "C2_MTF_COMBO_BREAKOUT": 3,
            "B2_DONCHIAN_INTRADAY": 4,
            "T2_RS_BTC_BREAKOUT": 5,
            "T3_KELTNER_TREND_4H": 6,
            "B3_VWAP_MOMENTUM_IMPULSE": 7,
            "B4_ORB_SESSION_BREAKOUT": 8,
            "B5_OI_TREND_EXPANSION": 9,
            "B6_EMA_PULLBACK_TREND": 10,
            "B7_FUNDING_SQUEEZE_MOMENTUM": 11,
            "S1_TREND_RETEST": 12,
        }
        approved_signals.sort(
            key=lambda s: (tier_ranks.get(s.strategy_id, 20), -s.ml_win_probability, -s.expected_return_pct)
        )
        demo_lab_fast = bool((self.settings.get("demo_lab") or {}).get("enabled", False))
        eff_max_signals = max(max_signals, 40) if demo_lab_fast else max_signals
        top_signals = approved_signals[:eff_max_signals]

        executed_orders: List[Dict[str, Any]] = []
        if auto_execute_demo:
            for sig in top_signals[: int(self.settings["risk"].get("max_concurrent_positions", 3))]:
                exec_res = self.demo_executor.execute_signal(sig)
                executed_orders.append(exec_res)

        screener_results.sort(key=lambda r: r.in_play_score, reverse=True)

        live_orders = []
        live_positions = []
        if self.client.api_key and self.client.api_secret:
            try:
                live_orders = self.client.get_open_orders()
                raw_pos = self.client.get_open_positions()
                live_positions = [p for p in raw_pos if float(p.get("size", 0) or 0) > 0]
            except Exception:
                pass

        carry_opportunities: List[Dict[str, Any]] = []
        if not demo_lab_fast and self.settings.get("strategy_mode", "s1_s3") != "legacy" and hasattr(self.client, "get_funding_history"):
            for sym in sorted(autotrade_set):
                if sym not in ctx_by_sym:
                    continue
                try:
                    f_hist = self.client.get_funding_history(sym, limit=12)
                    tk = ticker_map.get(sym, {})
                    perp_px = float(tk.get("lastPrice", 0) or 0)
                    spot_px = float(tk.get("indexPrice", 0) or perp_px)
                    opp = self.funding_carry.evaluate_pair(sym, f_hist, spot_px, perp_px)
                    carry_opportunities.append(asdict(opp))
                except Exception:
                    pass

        return {
            "context_errors": context_errors,
            "carry_opportunities": carry_opportunities,
            "data_source": self.client.last_data_source,
            "autotrade_liquid_window_active": in_liquid_hours,
            "live_open_orders": live_orders,
            "live_open_positions": live_positions,
            "network_note": self.client.last_network_error,
            "account_equity_usdt": equity_usd,
            "total_pairs_scanned": len(core_pairs),
            "coins_in_play_count": sum(1 for r in screener_results if r.passed),
            "raw_signals_generated": len(raw_candidates),
            "approved_sniper_signals_count": len(top_signals),
            "screener_top": [asdict(r) for r in screener_results],
            "approved_signals": [asdict(s) for s in top_signals],
            "rejected_signals": [asdict(s) for s in rejected_signals[:10]],
            "shadow_candidates": [asdict(s) for s in approved_signals],
            "strategy_diagnostics": diag,
            "executed_demo_orders": executed_orders,
        }

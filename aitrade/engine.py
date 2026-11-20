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
from aitrade.strategies.booster_breakout import generate_booster_breakout_signal
from aitrade.strategies.donchian_tsmom import generate_donchian_tsmom_signal
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
        c15 = self.client.get_klines(symbol, interval="15", limit=600)
        c1h = self.client.get_klines(symbol, interval="60", limit=220)
        c4h = self.client.get_klines(symbol, interval="240", limit=120)
        c5m = self.client.get_klines(symbol, interval="5", limit=60)
        orderbook = self.client.get_orderbook(symbol, limit=50)
        ob_hist = self.client.get_orderbook_history(symbol)
        oi_hist = self.client.get_open_interest_history(symbol, interval_time="15min", limit=40)
        recent_trades = self.client.get_recent_trades(symbol, limit=150)

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

        def evaluate_and_bucket(sig: TradeSignal, ctx: MarketContext) -> None:
            raw_candidates.append(sig)
            sig = self.ml_labeler.evaluate_signal(sig, ctx)
            sig = self.risk_engine.size_and_validate_signal(sig, ctx.orderbook, equity_usd)
            sig = self.debate_council.conduct_debate(sig, ctx)
            if sig.debate_verdict == "APPROVED" and sig.passed_ml_filter:
                approved_signals.append(sig)
            else:
                rejected_signals.append(sig)

        for sym, ctx in ctx_by_sym.items():
            sym_signals: List[TradeSignal] = []
            if self.settings.get("strategy_mode", "s1_s3") != "legacy":
                # 0) T1_DONCHIAN_TSMOM (Автотрейдинг Ядро 4h, 24/7, не зависит от 15m скальп-скринера RVOL)
                if sym in autotrade_set and len(ctx.candles_4h) >= 85 and ctx.screener.spread_pct <= 0.15:
                    t1_sig = generate_donchian_tsmom_signal(
                        sym,
                        ctx.candles_4h,
                        funding_rate=float(ctx.screener.funding_rate or 0.0),
                        horizons=(20, 40, 80),
                        target_ann_vol=0.25,
                        max_hold_bars=30,
                    )
                    if t1_sig is not None:
                        sym_signals.append(t1_sig)

            if ctx.screener.passed:
                if self.settings.get("strategy_mode", "s1_s3") == "legacy":
                    sym_signals.extend(evaluate_vwap_keltner_strategy(ctx))
                    sym_signals.extend(evaluate_liquidity_sweep_fvg_strategy(ctx))
                    sym_signals.extend(evaluate_oi_cvd_squeeze_strategy(ctx))
                    sym_signals.extend(evaluate_orb_breakout_strategy(ctx))
                    if sym != "BTCUSDT":
                        sym_signals.extend(evaluate_lead_lag_density_strategy(ctx))
                else:
                    best_bid = ctx.orderbook.best_bid
                    best_ask = ctx.orderbook.best_ask
                    # 1) S1_TREND_RETEST (Разгон B2) и S3_VWAP_RANGE (Автотрейдинг Рукав A, R:R >= 1.15)
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

                    # 2) B1_SQUEEZE_BREAKOUT (Разгон Ядро B1, с фильтром выходных NoWeekendChop)
                    if sym in booster_set:
                        b1_sig = generate_booster_breakout_signal(
                            sym,
                            ctx.candles_15m,
                            bars_1h=ctx.candles_1h,
                            funding_rate=ctx.screener.funding_rate,
                            obi_smoothed=ctx.micro.smoothed_obi,
                            best_bid=best_bid,
                            best_ask=best_ask,
                            exclude_weekend_chop=True,
                        )
                        if b1_sig is not None:
                            sym_signals.append(b1_sig)

            for sig in sym_signals:
                evaluate_and_bucket(sig, ctx)

        # 3) Рукав B Автотрейдинга: кросс-секционный «Моментум + Фандинг» (только в ликвидные часы UTC)
        if in_liquid_hours and len(h1_autotrade) >= 2 and self.settings.get("strategy_mode", "s1_s3") != "legacy":
            mom_sigs = generate_momentum_funding_signals(h1_autotrade, fr_autotrade, top_n=1)
            for m_sig in mom_sigs:
                m_ctx = ctx_by_sym.get(m_sig.symbol)
                if m_ctx is not None:
                    evaluate_and_bucket(m_sig, m_ctx)

        approved_signals.sort(
            key=lambda s: (s.ml_win_probability, s.expected_return_pct), reverse=True
        )
        top_signals = approved_signals[:max_signals]

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
        if self.settings.get("strategy_mode", "s1_s3") != "legacy" and hasattr(self.client, "get_funding_history"):
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
            "executed_demo_orders": executed_orders,
        }

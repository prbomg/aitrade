"""
Главный оркестратор ИИ-комбайна (AITradeCombineEngine):
Объединяет все 4 эшелона воронки отбора для получения 3–10 снайперских сделок в день с максимальным винрейтом:
1. Подключение к Bybit V5 (реальные свечи 5m/15m/1h/4h, глубокий стакан Level-2, Open Interest, Funding, Recent Trades).
2. Четырехступенчатый пре-скринер «Монеты в игре» (Coins in Play: RVOL, NATR, Spread < 0.10%, MTF Bias).
3. Генерация кандидатов по 5 интрадей-архетипам (VWAP/Keltner ±2σ, Liquidity Sweep + FVG, OI/CVD Squeeze, Session ORB, Lead-Lag + L2 Density).
4. Валидация через табличное ML-ядро (Walk-Forward Meta-Labeling + Dissimilarity Index <= 1.0 + Cost-Aware Filter lambda=2.0),
   раунд дебатов Бычьего и Медвежьего ИИ-агентов (TradingAgents Debate Council) и расчет позиции в RiskEngine (5x Liq Rule + 10-Level L2 Slippage).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from aitrade.agents.debate_council import MultiAgentDebateCouncil
from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import BybitV5Client
from aitrade.exchange.coinalyze import CoinalyzeClient
from aitrade.execution.demo_trader import BybitDemoExecutor
from aitrade.features.indicators import calc_session_vwap_bands, calc_volume_profile
from aitrade.features.microstructure import analyze_microstructure
from aitrade.ml.meta_labeler import WalkForwardMetaLabeler
from aitrade.risk.risk_engine import RiskEngine
from aitrade.screener.coins_in_play import CoinsInPlayScreener, ScreenerResult
from aitrade.strategies.base import MarketContext, TradeSignal
from aitrade.strategies.lead_lag_density import evaluate_lead_lag_density_strategy
from aitrade.strategies.liquidity_sweep_fvg import evaluate_liquidity_sweep_fvg_strategy
from aitrade.strategies.oi_cvd_squeeze import evaluate_oi_cvd_squeeze_strategy
from aitrade.strategies.orb_breakout import evaluate_orb_breakout_strategy
from aitrade.strategies.regime_s1_s3 import generate_s1_s3_signal
from aitrade.strategies.vwap_keltner_mr import evaluate_vwap_keltner_strategy


class AITradeCombineEngine:
    """Центральное ядро ИИ-комбайна AItrade для Bybit V5."""

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or load_settings()
        self.client = BybitV5Client(self.settings, allow_offline_fallback=bool(self.settings.get("offline_simulation", False)))
        self.coinalyze = CoinalyzeClient(self.settings.get("coinalyze_api_key", ""))
        self.screener = CoinsInPlayScreener(self.settings)
        self.ml_labeler = WalkForwardMetaLabeler(self.settings)
        self.debate_council = MultiAgentDebateCouncil(self.settings)
        self.risk_engine = RiskEngine(self.settings)
        self.demo_executor = BybitDemoExecutor(self.client, self.settings)

    def build_market_context(
        self,
        symbol: str,
        ticker_map: Dict[str, Dict[str, Any]],
        btc_candles_15m: List[Any],
    ) -> MarketContext:
        ticker = ticker_map.get(symbol, {"symbol": symbol})
        c15 = self.client.get_klines(symbol, interval="15", limit=600)
        c1h = self.client.get_klines(symbol, interval="60", limit=80)
        c4h = self.client.get_klines(symbol, interval="240", limit=50)
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
        core_pairs = symbols or self.settings["universe"]["core_pairs"]
        all_tickers = self.client.get_tickers()
        ticker_map = {t["symbol"]: t for t in all_tickers if "symbol" in t}
        btc_15m = self.client.get_klines("BTCUSDT", interval="15", limit=120)
        equity_usd = min(self.demo_executor.get_account_equity_usdt(), float(self.settings.get("risk", {}).get("allocated_equity_usdt", 1000.0)))

        screener_results: List[ScreenerResult] = []
        raw_candidates: List[TradeSignal] = []
        approved_signals: List[TradeSignal] = []
        rejected_signals: List[TradeSignal] = []

        max_workers = min(12, max(1, len(core_pairs)))
        context_errors = []
        def build_one(symbol):
            try:
                ctx = self.build_market_context(symbol, ticker_map, btc_15m)
                if len(ctx.candles_15m) < 45 or not ctx.orderbook.bids or not ctx.orderbook.asks:
                    raise ValueError("Недостаточно рыночных данных")
                return symbol, ctx
            except Exception as exc:
                context_errors.append({"symbol": symbol, "error": str(exc)[:160]})
                return symbol, None
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            contexts = list(
                pool.map(
                    build_one,
                    core_pairs,
                )
            )

        for sym, ctx in contexts:
            if ctx is None: continue
            screener_results.append(ctx.screener)
            if not ctx.screener.passed:
                continue

            sym_signals: List[TradeSignal] = []
            if self.settings.get("strategy_mode", "s1_s3") == "legacy":
                sym_signals.extend(evaluate_vwap_keltner_strategy(ctx))
                sym_signals.extend(evaluate_liquidity_sweep_fvg_strategy(ctx))
                sym_signals.extend(evaluate_oi_cvd_squeeze_strategy(ctx))
                sym_signals.extend(evaluate_orb_breakout_strategy(ctx))
                if sym != "BTCUSDT":
                    sym_signals.extend(evaluate_lead_lag_density_strategy(ctx))
            else:
                s1_s3_sig = generate_s1_s3_signal(
                    sym,
                    ctx.candles_15m,
                    bars_1h=ctx.candles_1h,
                    min_rr_s3=0.50,
                    obi_smoothed=ctx.micro.smoothed_obi,
                )
                if s1_s3_sig is not None:
                    sym_signals.append(s1_s3_sig)

            for sig in sym_signals:
                raw_candidates.append(sig)
                sig = self.ml_labeler.evaluate_signal(sig, ctx)
                sig = self.risk_engine.size_and_validate_signal(sig, ctx.orderbook, equity_usd)
                sig = self.debate_council.conduct_debate(sig, ctx)

                if sig.debate_verdict == "APPROVED" and sig.passed_ml_filter:
                    approved_signals.append(sig)
                else:
                    rejected_signals.append(sig)

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

        return {
            "context_errors": context_errors,
            "data_source": self.client.last_data_source,
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

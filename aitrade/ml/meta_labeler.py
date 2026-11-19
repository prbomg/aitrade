"""
Уровень 2 ИИ-комбайна: Табличное ML-ядро мета-разметки (Meta-Labeling),
расчета Индекса несходства (Dissimilarity Index — DI) и фильтра окупаемости издержек (Cost-Aware Execution Filter).
Реализует:
1. Обучение на скользящем окне (Walk-Forward) ансамбля мета-разметки на чистом Python
   с экспоненциальным весом свежих наблюдений exp(-i / (alpha * n)).
2. Нормализацию признаков в диапазон [-1, 1] (MinMaxScaler на объединенном окне) и расчет Dissimilarity Index:
   DI_k = d_min(x_k, X_train) / d_bar(X_train). Если DI_k > DI_threshold (1.0), сигнал блокируется как рыночный шок.
3. Cost-Aware Execution Filter: |r_hat| > lambda * c_total (где lambda = 2.0, а c_total включает комиссию входа/выхода
   на Bybit и ожидаемый спред/проскальзывание). Отсекает шумовые входы.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series, calc_rvol, calc_rsi, calc_session_vwap_bands
from aitrade.strategies.base import MarketContext, TradeSignal


class WalkForwardMetaLabeler:
    """Адаптивный ML-валидатор сигналов с защитой от выбросов (DI) и фильтром издержек (Cost-Aware)."""

    def __init__(self, settings: Dict[str, Any]) -> None:
        ml_cfg = settings.get("ml_filter", {})
        ex_cfg = settings.get("exchange", {})
        self.cost_lambda = float(ml_cfg.get("cost_selectivity_lambda", 2.0))
        self.di_threshold = float(ml_cfg.get("di_threshold", 1.0))
        self.min_win_prob = float(ml_cfg.get("min_win_probability", 0.62))
        self.label_horizon = int(ml_cfg.get("label_horizon_bars", 8))
        self.maker_fee = float(ex_cfg.get("maker_fee_rate", 0.0002))
        self.taker_fee = float(ex_cfg.get("taker_fee_rate", 0.00055))
        self._candidate_samples=[]

    def _extract_historical_matrix(
        self, candles: List[Candle], direction_sign: float, strategy_id=None
    ) -> Tuple[List[List[float]], List[float], List[float]]:
        """
        Строит обучающую выборку исторических векторов признаков X, бинарных меток успеха y (1/0)
        и реализованных амплитуд доходности r_realized на горизонте label_horizon свечей.
        Все 4 координаты строго согласованы по физическому смыслу с x_new:
        [vwap_zscore_capped, rsi_norm, rvol_norm, clv_obi_proxy].
        """
        n = len(candles)
        if n < 45:
            return [], [], []

        from aitrade.strategies.regime_s1_s3 import _build_ml_features,generate_s1_s3_signal
        from aitrade.backtest.walk_forward import WalkForwardBacktester
        replay=WalkForwardBacktester({'exchange':{'maker_fee_rate':self.maker_fee,'taker_fee_rate':self.taker_fee}})
        atr_s=calc_atr_series(candles,14)
        X=[];y=[];returns_pct=[];self._candidate_samples=[]
        intraday=strategy_id in ('S1_TREND_RETEST','S3_VWAP_RANGE')
        start_idx=219 if intraday else 18
        end_idx=n-(27 if intraday else self.label_horizon)
        last_exit=0
        for i in range(start_idx,end_idx):
            feat=self._feature_vector(candles[:i+1])
            if intraday:
                if candles[i].timestamp<last_exit:continue
                sig=generate_s1_s3_signal('TRAIN',candles[:i+1],reference_open_px=candles[i+1].open,min_rr_s3=.5)
                if not sig or sig.strategy_id!=strategy_id or (sig.side=='Buy')!=(direction_sign>0):continue
                trade=replay.replay_signal(candles,i+1,sig)
                if not trade:continue
                realized=trade[1];last_exit=trade[4]
            else:
                entry=candles[i].close
                target=max(.0022,atr_s[i]/entry*.85);stop=target/1.15
                sl=entry*(1-stop*direction_sign);tp=entry*(1+target*direction_sign)
                realized,_,_=replay._resolve_trade(candles[i+1:i+1+self.label_horizon],
                    'Buy' if direction_sign>0 else 'Sell',entry,sl,tp,self.taker_fee+.0003,self.taker_fee+.0003,self.taker_fee+.0003,False)
            X.append(feat);y.append(float(realized>0));returns_pct.append(realized)
            if intraday:self._candidate_samples.append({'bar_ms':candles[i].timestamp,'end_ms':last_exit,'x':feat,'y':float(realized>0),'ret':realized})

        return X, y, returns_pct

    @staticmethod
    def _feature_vector(candles):
        from aitrade.strategies.regime_s1_s3 import _build_ml_features
        f=_build_ml_features(candles)
        return [max(-1.8,min(1.8,f['vwap_zscore'])),(f['rsi_14']-50)/50,min(f['rvol'],2.2)/2.2,f['clv_proxy']]

    @staticmethod
    def _load_ledger_closed_samples(strategy_id: str, side: str) -> Tuple[List[List[float]], List[float], List[float]]:
        """Загружает реально закрытые и атрибутированные сделки из execution_ledger.sqlite3 (Closed-Loop Learning)."""
        try:
            from aitrade.config import BASE_DIR
            from aitrade.storage import Ledger
            db_path = BASE_DIR / "data" / "execution_ledger.sqlite3"
            if not db_path.exists():
                return [], [], []
            ledger = Ledger(db_path)
            closed_rows = ledger.attribute(ledger.records("closed"))
            intents_map, _, _ = ledger._execution_lifecycles()
            lx: List[List[float]] = []
            ly: List[float] = []
            lr: List[float] = []
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
                # Добавляем реальный боевой опыт с весом 2x
                for _ in range(2):
                    lx.append(vec)
                    ly.append(1.0 if pnl > 0 else 0.0)
                    lr.append(ret_pct)
            return lx, ly, lr
        except Exception:
            return [], [], []

    def _compute_dissimilarity_index(
        self, X_train: List[List[float]], x_new: List[float]
    ) -> float:
        """
        Формула Индекса несходства FreqAI:
        DI_k = d_min(x_k, X_train) / d_bar(X_train) после нормализации MinMaxScaler(-1, 1).
        """
        if len(X_train) < 5:
            return 0.45
        dim = len(x_new)
        all_rows = X_train
        mins = [min(row[d] for row in all_rows) for d in range(dim)]
        maxs = [max(row[d] for row in all_rows) for d in range(dim)]

        def scale(vec: List[float]) -> List[float]:
            out: List[float] = []
            for d in range(dim):
                span = maxs[d] - mins[d]
                if span <= 1e-9:
                    out.append(0.0)
                else:
                    norm01 = (vec[d] - mins[d]) / span
                    out.append(norm01 * 2.0 - 1.0)
            return out

        scaled_train = [scale(r) for r in X_train]
        scaled_new = scale(x_new)

        def euclid(a: List[float], b: List[float]) -> float:
            return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

        pair_dists: List[float] = []
        m = len(scaled_train)
        for i in range(m):
            for j in range(i + 1, m):
                pair_dists.append(euclid(scaled_train[i], scaled_train[j]))
        d_bar = sum(pair_dists) / max(len(pair_dists), 1)
        if d_bar <= 1e-9:
            return 0.50

        d_min = min(euclid(scaled_new, row) for row in scaled_train)
        return round(d_min / d_bar, 3)

    def evaluate_signal(self, signal: TradeSignal, ctx: MarketContext) -> TradeSignal:
        """
        Пропускает сгенерированный стратегией сигнал через:
        1. Walk-Forward оценку вероятности успеха (Meta-Labeling Win Probability),
        2. Проверку аномалий по индексу несходства (DI <= DI_threshold),
        3. Cost-Aware Execution Filter: |r_hat| > lambda * c_total.
        """
        direction_sign = 1.0 if signal.side == "Buy" else -1.0
        X_train, y_train, ret_train = self._extract_historical_matrix(ctx.candles_15m, direction_sign, signal.strategy_id)

        if signal.strategy_id in ('S1_TREND_RETEST','S3_VWAP_RANGE'):
            from aitrade.config import BASE_DIR
            from aitrade.storage import atomic_json
            import json
            path=BASE_DIR/'data'/'ml_samples_v2.json'
            saved=json.loads(path.read_text()) if path.exists() else {}
            for sample in self._candidate_samples:
                key=f"{signal.strategy_id}:{signal.side}:{signal.symbol}:{sample['bar_ms']}"
                saved[key]=dict(sample,strategy=signal.strategy_id,side=signal.side,symbol=signal.symbol)
            saved=dict(sorted(saved.items(),key=lambda kv:kv[1]['end_ms'])[-4000:])
            atomic_json(path,saved)
            eligible=[v for v in saved.values() if v['strategy']==signal.strategy_id and v['side']==signal.side and v['end_ms']<=ctx.candles_15m[-1].timestamp+900000][-256:]
            X_train=[v['x'] for v in eligible];y_train=[v['y'] for v in eligible];ret_train=[v['ret'] for v in eligible]
        lx, ly, lr = self._load_ledger_closed_samples(signal.strategy_id, signal.side)
        if lx:
            X_train = (X_train + lx)[-256:]
            y_train = (y_train + ly)[-256:]
            ret_train = (ret_train + lr)[-256:]
        signal.features['ml_live_ledger_samples'] = len(lx) // 2
        # Historical OHLCV and live OHLCV use the exact same builder. Live L2 stays a separate gate.
        x_new=self._feature_vector(ctx.candles_15m)
        signal.features['ml_feature_version']=2
        signal.features['ml_training_samples']=len(X_train)
        signal.features['ml_calibration_status']='uncalibrated_similarity_score'

        di_value = self._compute_dissimilarity_index(X_train, x_new)

        if X_train and y_train:
            total_w = 0.0
            win_w = 0.0
            ret_w = 0.0
            n_samples = len(X_train)
            for idx, (row, label, r_pct) in enumerate(zip(X_train, y_train, ret_train)):
                recency_w = math.exp(-(n_samples - idx) / (0.85 * n_samples))
                dist_sq = sum((a - b) ** 2 for a, b in zip(row, x_new))
                sim_w = math.exp(-dist_sq / 2.0)
                w = recency_w * sim_w
                total_w += w
                win_w += w * label
                ret_w += w * r_pct
            empirical_prob = win_w / max(total_w, 1e-9)
            empirical_ret = ret_w / max(total_w, 1e-9)
        else:
            empirical_prob = signal.base_winrate_est
            empirical_ret = 1.25

        micro_bonus = 0.0
        if direction_sign * ctx.micro.smoothed_obi > 0.08:
            micro_bonus += 0.045
        elif direction_sign * ctx.micro.smoothed_obi < -0.15:
            micro_bonus -= 0.060

        if direction_sign * ctx.micro.vamp_divergence_bps > 0.15:
            micro_bonus += 0.025
        if ctx.screener.rvol >= 1.5:
            micro_bonus += 0.025
        if ctx.micro.liquidity_behavior_index >= 0.80:
            micro_bonus += 0.025
        if signal.reward_to_risk_tp1 >= 1.6:
            micro_bonus += 0.025

        blended_prob = 0.68 * signal.base_winrate_est + 0.32 * empirical_prob + micro_bonus
        blended_prob = round(max(0.35, min(0.92, blended_prob)), 4)

        target_tp1_pct = abs(signal.take_profit_1 - signal.entry_price) / max(signal.entry_price, 1e-9) * 100.0
        expected_return_pct = round(
            blended_prob * target_tp1_pct
            - (1.0 - blended_prob) * (signal.stop_distance_pct),
            3,
        )

        entry_fee_pct = self.taker_fee * 100.0
        exit_fee_pct = self.taker_fee * 100.0
        total_roundtrip_cost_pct = entry_fee_pct + exit_fee_pct + ctx.screener.spread_pct + 0.06
        cost_threshold_pct = round(self.cost_lambda * total_roundtrip_cost_pct, 4)

        req_prob = 0.52 if signal.strategy_id == "S1_TREND_RETEST" else (0.56 if signal.strategy_id == "S3_VWAP_RANGE" else self.min_win_prob)
        passed_cost = expected_return_pct >= cost_threshold_pct
        passed_di = di_value <= self.di_threshold
        passed_prob = blended_prob >= req_prob

        signal.ml_win_probability = blended_prob
        signal.expected_return_pct = expected_return_pct
        signal.cost_threshold_pct = cost_threshold_pct
        signal.dissimilarity_index = di_value
        min_samples = min(8, len(X_train)) if len(X_train) > 0 else 0
        signal.passed_ml_filter = bool(passed_cost and passed_di and passed_prob and (len(X_train)>=min_samples if signal.strategy_id in ("S1_TREND_RETEST","S3_VWAP_RANGE") else True))
        return signal

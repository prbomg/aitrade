"""
Уровень 2 ИИ-комбайна (AItrade 2026): Реальный Meta-Labeling с разметкой Triple Barrier,
Purged K-Fold + Embargo (1%), вероятностной калибровкой (Platt / CalibratedClassifierCV)
и честным Cost-Aware Execution Filter без захардкоженных констант base_winrate_est.
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional, Tuple

from aitrade.exchange.bybit_v5 import Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.base import MarketContext, TradeSignal

try:
    import numpy as np  # type: ignore
    from sklearn.calibration import CalibratedClassifierCV  # type: ignore
    from sklearn.linear_model import LogisticRegression  # type: ignore
    from sklearn.preprocessing import StandardScaler  # type: ignore

    SKLEARN_AVAILABLE = True
except Exception:
    np = None  # type: ignore
    CalibratedClassifierCV = None  # type: ignore
    LogisticRegression = None  # type: ignore
    StandardScaler = None  # type: ignore
    SKLEARN_AVAILABLE = False

try:
    from lightgbm import LGBMClassifier  # type: ignore

    LIGHTGBM_AVAILABLE = True
except Exception:
    LGBMClassifier = None  # type: ignore
    LIGHTGBM_AVAILABLE = False


def purged_kfold_splits(
    intervals: List[Tuple[int, int]],
    n_splits: int = 5,
    embargo_pct: float = 0.01,
) -> List[Tuple[List[int], List[int]]]:
    """
    Purged K-Fold / CPCV:
    - Удаляет из train любые наблюдения, чей интервал [start_ms, end_ms] пересекается с test-фолдом.
    - Добавляет буфер embargo (по умолчанию 1% выборки) сразу после test-фолда.
    """
    n = len(intervals)
    if n < max(4, n_splits):
        return []
    fold_size = n // n_splits
    embargo = max(1, int(math.ceil(n * embargo_pct)))
    splits: List[Tuple[List[int], List[int]]] = []

    for f in range(n_splits):
        t_start = f * fold_size
        t_end = (f + 1) * fold_size if f < n_splits - 1 else n
        test_idx = list(range(t_start, t_end))
        if not test_idx:
            continue
        test_min_ms = min(intervals[i][0] for i in test_idx)
        test_max_ms = max(intervals[i][1] for i in test_idx)
        embargo_end = min(n, t_end + embargo)

        train_idx: List[int] = []
        for i in range(n):
            if t_start <= i < embargo_end:
                continue
            s_ms, e_ms = intervals[i]
            # Purging: исключаем любое перекрытие по времени с тестовым фолдом
            if e_ms >= test_min_ms and s_ms <= test_max_ms:
                continue
            train_idx.append(i)
        if train_idx and test_idx:
            splits.append((train_idx, test_idx))
    return splits


def _sigmoid(z: float) -> float:
    if z >= 35.0:
        return 1.0
    if z <= -35.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


class _PurePythonCalibratedLogReg:
    """
    Zero-dependency L2-регуляризованная логистическая регрессия + Platt-калибровка (a*logit + b)
    на out-of-fold прогнозах Purged K-Fold.
    """

    def __init__(self, l2: float = 0.5, lr: float = 0.08, epochs: int = 180) -> None:
        self.l2 = l2
        self.lr = lr
        self.epochs = epochs
        self.means: List[float] = []
        self.stds: List[float] = []
        self.weights: List[float] = []
        self.bias: float = 0.0
        self.platt_a: float = 1.0
        self.platt_b: float = 0.0

    def _fit_standardizer(self, X: List[List[float]]) -> List[List[float]]:
        dim = len(X[0])
        n = len(X)
        self.means = [sum(r[d] for r in X) / n for d in range(dim)]
        self.stds = [
            max(1e-6, math.sqrt(sum((r[d] - self.means[d]) ** 2 for r in X) / n))
            for d in range(dim)
        ]
        return [[(r[d] - self.means[d]) / self.stds[d] for d in range(dim)] for r in X]

    def _transform(self, x: List[float]) -> List[float]:
        return [(x[d] - self.means[d]) / self.stds[d] for d in range(len(self.means))]

    @staticmethod
    def _fit_raw_logreg(
        Xs: List[List[float]], y: List[float], l2: float, lr: float, epochs: int
    ) -> Tuple[List[float], float]:
        dim = len(Xs[0])
        n = len(Xs)
        w = [0.0] * dim
        b = 0.0
        for _ in range(epochs):
            gw = [0.0] * dim
            gb = 0.0
            for row, label in zip(Xs, y):
                p = _sigmoid(sum(wi * xi for wi, xi in zip(w, row)) + b)
                err = p - label
                gb += err
                for d in range(dim):
                    gw[d] += err * row[d]
            inv_n = 1.0 / max(n, 1)
            b -= lr * gb * inv_n
            for d in range(dim):
                w[d] -= lr * (gw[d] * inv_n + l2 * w[d] * inv_n)
        return w, b

    def fit(
        self,
        X: List[List[float]],
        y: List[float],
        intervals: List[Tuple[int, int]],
    ) -> None:
        Xs = self._fit_standardizer(X)
        self.weights, self.bias = self._fit_raw_logreg(Xs, y, self.l2, self.lr, self.epochs)

        # Out-of-fold Platt-калибровка на Purged K-Fold
        splits = purged_kfold_splits(intervals, n_splits=5, embargo_pct=0.01)
        oof_logits: List[float] = []
        oof_y: List[float] = []
        for tr_idx, te_idx in splits:
            x_tr = [Xs[i] for i in tr_idx]
            y_tr = [y[i] for i in tr_idx]
            if len(set(y_tr)) < 2:
                continue
            w_f, b_f = self._fit_raw_logreg(x_tr, y_tr, self.l2, self.lr, self.epochs // 2)
            for i in te_idx:
                oof_logits.append(sum(wi * xi for wi, xi in zip(w_f, Xs[i])) + b_f)
                oof_y.append(y[i])

        if len(oof_logits) >= 20 and len(set(oof_y)) >= 2:
            a, b_p = 1.0, 0.0
            n_oof = len(oof_logits)
            for _ in range(120):
                ga = 0.0
                gb = 0.0
                for z, lbl in zip(oof_logits, oof_y):
                    p = _sigmoid(a * z + b_p)
                    err = p - lbl
                    ga += err * z
                    gb += err
                a -= 0.05 * (ga / n_oof + 0.05 * (a - 1.0))
                b_p -= 0.05 * (gb / n_oof)
            self.platt_a = max(0.1, min(5.0, a))
            self.platt_b = max(-3.0, min(3.0, b_p))

    def predict_proba(self, x: List[float]) -> float:
        xs = self._transform(x)
        logit = sum(wi * xi for wi, xi in zip(self.weights, xs)) + self.bias
        return _sigmoid(self.platt_a * logit + self.platt_b)


class WalkForwardMetaLabeler:
    """
    Реальный ML-валидатор сигналов (Meta-Labeling):
    - Triple Barrier разметка (TP / SL / вертикальный барьер).
    - Purged K-Fold (с purging и 1% embargo) + вероятностная калибровка.
    - Честный холодный старт: при < 100 сэмплах статус WARMING_UP без вымышленного WR.
    - Строгий Cost-Aware Execution Filter по откалиброванной P_cal(win) и реальному R:R.
    """

    MIN_CALIBRATION_SAMPLES = 100
    WFA_APPROVED_STRATEGIES = {
        "S1_TREND_RETEST",
        "S3_VWAP_RANGE",
        "B1_SQUEEZE_BREAKOUT",
        "AUTOTRADE_MOMENTUM_FUNDING",
    }

    def __init__(self, settings: Dict[str, Any]) -> None:
        ml_cfg = settings.get("ml_filter", {})
        ex_cfg = settings.get("exchange", {})
        self.cost_lambda = float(ml_cfg.get("cost_selectivity_lambda", 1.5))
        self.di_threshold = float(ml_cfg.get("di_threshold", 1.25))
        self.min_win_prob = float(ml_cfg.get("min_win_probability", 0.58))
        self.min_samples = int(ml_cfg.get("min_training_samples", self.MIN_CALIBRATION_SAMPLES))
        self.label_horizon = int(ml_cfg.get("label_horizon_bars", 8))
        self.maker_fee = float(ex_cfg.get("maker_fee_rate", 0.0002))
        self.taker_fee = float(ex_cfg.get("taker_fee_rate", 0.00055))
        self._candidate_samples: List[Dict[str, Any]] = []

    def _extract_historical_matrix(
        self, candles: List[Candle], direction_sign: float, strategy_id: Optional[str] = None
    ) -> Tuple[List[List[float]], List[float], List[float]]:
        """
        Строит историческую выборку признаков X, бинарных меток Triple Barrier y (1/0)
        и реализованных доходностей r_realized.
        """
        n = len(candles)
        if n < 45:
            return [], [], []

        from aitrade.strategies.regime_s1_s3 import generate_s1_s3_signal
        from aitrade.backtest.walk_forward import WalkForwardBacktester

        replay = WalkForwardBacktester(
            {"exchange": {"maker_fee_rate": self.maker_fee, "taker_fee_rate": self.taker_fee}}
        )
        atr_s = calc_atr_series(candles, 14)
        X: List[List[float]] = []
        y: List[float] = []
        returns_pct: List[float] = []
        self._candidate_samples = []

        intraday = strategy_id in ("S1_TREND_RETEST", "S3_VWAP_RANGE")
        start_idx = 219 if intraday else 18
        end_idx = n - (27 if intraday else self.label_horizon)
        last_exit = 0

        for i in range(start_idx, end_idx):
            feat = self._feature_vector(candles[: i + 1])
            if intraday:
                if candles[i].timestamp < last_exit:
                    continue
                sig = generate_s1_s3_signal(
                    "TRAIN", candles[: i + 1], reference_open_px=candles[i + 1].open, min_rr_s3=1.15
                )
                if not sig or sig.strategy_id != strategy_id or (sig.side == "Buy") != (direction_sign > 0):
                    continue
                trade = replay.replay_signal(candles, i + 1, sig)
                if not trade:
                    continue
                realized = trade[1]
                last_exit = trade[4]
                end_ms = last_exit
            else:
                entry = candles[i].close
                target = max(0.0022, atr_s[i] / entry * 0.85)
                stop = target / 1.15
                sl = entry * (1.0 - stop * direction_sign)
                tp = entry * (1.0 + target * direction_sign)
                realized, _, used = replay._resolve_trade(
                    candles[i + 1 : i + 1 + self.label_horizon],
                    "Buy" if direction_sign > 0 else "Sell",
                    entry,
                    sl,
                    tp,
                    self.taker_fee + 0.0003,
                    self.taker_fee + 0.0003,
                    self.taker_fee + 0.0003,
                    False,
                )
                end_ms = int(candles[min(i + used, n - 1)].timestamp)

            # Triple Barrier label: 1 только если чистая доходность после издержек > 0
            label = 1.0 if realized > 0.0 else 0.0
            X.append(feat)
            y.append(label)
            returns_pct.append(realized)
            self._candidate_samples.append({
                "bar_ms": int(candles[i].timestamp),
                "end_ms": int(end_ms),
                "x": feat,
                "y": label,
                "ret": realized,
            })

        return X, y, returns_pct

    @staticmethod
    def _feature_vector(candles: List[Candle]) -> List[float]:
        from aitrade.strategies.regime_s1_s3 import _build_ml_features

        f = _build_ml_features(candles)
        return [
            max(-1.8, min(1.8, f["vwap_zscore"])),
            (f["rsi_14"] - 50.0) / 50.0,
            min(f["rvol"], 2.2) / 2.2,
            f["clv_proxy"],
        ]

    @staticmethod
    def _load_ledger_closed_samples(
        strategy_id: str, side: str
    ) -> Tuple[List[List[float]], List[float], List[float], List[Tuple[int, int]]]:
        """Загружает реально закрытые и атрибутированные сделки из execution_ledger.sqlite3."""
        try:
            from aitrade.config import BASE_DIR
            from aitrade.storage import Ledger

            db_path = BASE_DIR / "data" / "execution_ledger.sqlite3"
            if not db_path.exists():
                return [], [], [], []
            ledger = Ledger(db_path)
            closed_rows = ledger.attribute(ledger.records("closed"))
            intents_map, _, _ = ledger._execution_lifecycles()
            lx: List[List[float]] = []
            ly: List[float] = []
            lr: List[float] = []
            li: List[Tuple[int, int]] = []
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
                s_ms = int(r.get("entry_created_ms") or r.get("createdTime") or 0)
                e_ms = int(r.get("updatedTime") or s_ms + 900_000)
                lx.append(vec)
                ly.append(1.0 if pnl > 0 else 0.0)
                lr.append(ret_pct)
                li.append((s_ms, max(s_ms, e_ms)))
            return lx, ly, lr, li
        except Exception:
            return [], [], [], []

    def _compute_dissimilarity_index(
        self, X_train: List[List[float]], x_new: List[float]
    ) -> float:
        """DI_k = d_min(x_k, X_train) / d_bar(X_train) после нормализации MinMaxScaler(-1, 1)."""
        if len(X_train) < 5:
            return 0.45
        dim = len(x_new)
        mins = [min(row[d] for row in X_train) for d in range(dim)]
        maxs = [max(row[d] for row in X_train) for d in range(dim)]

        def scale(vec: List[float]) -> List[float]:
            out: List[float] = []
            for d in range(dim):
                span = maxs[d] - mins[d]
                out.append(0.0 if span <= 1e-9 else ((vec[d] - mins[d]) / span) * 2.0 - 1.0)
            return out

        scaled_train = [scale(r) for r in X_train[-128:]]
        scaled_new = scale(x_new)

        def euclid(a: List[float], b: List[float]) -> float:
            return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

        m = len(scaled_train)
        pair_dists = [
            euclid(scaled_train[i], scaled_train[j])
            for i in range(m)
            for j in range(i + 1, m)
        ]
        d_bar = sum(pair_dists) / max(len(pair_dists), 1)
        if d_bar <= 1e-9:
            return 0.50
        d_min = min(euclid(scaled_new, row) for row in scaled_train)
        return round(d_min / d_bar, 3)

    def fit_predict_calibrated_prob(
        self,
        X_train: List[List[float]],
        y_train: List[float],
        intervals: List[Tuple[int, int]],
        x_new: List[float],
    ) -> Tuple[float, str]:
        """
        Обучает калиброванный мета-классификатор на X_train/y_train с Purged K-Fold.
        Возвращает (P_cal(win), backend_name).
        """
        if not X_train or len(y_train) != len(X_train):
            return 0.5, "empty"
        pos = sum(1 for v in y_train if v > 0.5)
        if pos == 0:
            return 0.05, "degenerate_all_loss"
        if pos == len(y_train):
            return 0.95, "degenerate_all_win"

        if SKLEARN_AVAILABLE:
            try:
                X_np = np.asarray(X_train, dtype=float)
                y_np = np.asarray([int(v > 0.5) for v in y_train], dtype=int)
                x_np = np.asarray([x_new], dtype=float)
                scaler = StandardScaler()
                X_sc = scaler.fit_transform(X_np)
                x_sc = scaler.transform(x_np)
                if LIGHTGBM_AVAILABLE:
                    base_clf = LGBMClassifier(
                        n_estimators=60,
                        max_depth=3,
                        learning_rate=0.05,
                        min_child_samples=10,
                        random_state=42,
                        verbose=-1,
                    )
                    backend = "lightgbm_calibrated"
                else:
                    base_clf = LogisticRegression(C=0.5, max_iter=300, random_state=42)
                    backend = "sklearn_logreg_calibrated"
                splits = purged_kfold_splits(intervals, n_splits=5, embargo_pct=0.01)
                valid_cv = [(tr, te) for tr, te in splits if len(set(y_np[tr])) >= 2]
                if len(valid_cv) >= 2:
                    cal = CalibratedClassifierCV(base_clf, method="sigmoid", cv=valid_cv)
                    cal.fit(X_sc, y_np)
                    prob = float(cal.predict_proba(x_sc)[0][1])
                else:
                    base_clf.fit(X_sc, y_np)
                    prob = float(base_clf.predict_proba(x_sc)[0][1])
                return round(max(0.02, min(0.98, prob)), 4), backend
            except Exception:
                pass

        clf = _PurePythonCalibratedLogReg()
        clf.fit(X_train, y_train, intervals)
        prob = clf.predict_proba(x_new)
        return round(max(0.02, min(0.98, prob)), 4), "pure_python_platt_logreg"

    def evaluate_signal(
        self,
        signal: TradeSignal,
        ctx: MarketContext,
        wfa_validated: Optional[bool] = None,
    ) -> TradeSignal:
        """
        Оценивает сигнал через реальный Meta-Labeling (без 0.68*base_winrate_est):
        - При < min_samples (100) возвращает статус WARMING_UP и пропускает только WFA-валидированные стратегии.
        - При >= min_samples применяет калиброванный классификатор + DI + строгий Cost-Aware Filter.
        """
        direction_sign = 1.0 if signal.side == "Buy" else -1.0
        X_train, y_train, ret_train = self._extract_historical_matrix(
            ctx.candles_15m, direction_sign, signal.strategy_id
        )
        intervals: List[Tuple[int, int]] = [
            (s["bar_ms"], s["end_ms"]) for s in self._candidate_samples
        ]

        if signal.strategy_id in ("S1_TREND_RETEST", "S3_VWAP_RANGE", "B1_SQUEEZE_BREAKOUT"):
            from aitrade.config import BASE_DIR
            from aitrade.storage import atomic_json

            path = BASE_DIR / "data" / "ml_samples_v2.json"
            saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            for sample in self._candidate_samples:
                key = f"{signal.strategy_id}:{signal.side}:{signal.symbol}:{sample['bar_ms']}"
                saved[key] = dict(sample, strategy=signal.strategy_id, side=signal.side, symbol=signal.symbol)
            saved = dict(sorted(saved.items(), key=lambda kv: kv[1]["end_ms"])[-4000:])
            atomic_json(path, saved)
            cutoff_ms = ctx.candles_15m[-1].timestamp + 900_000
            eligible = [
                v
                for v in saved.values()
                if v["strategy"] == signal.strategy_id
                and v["side"] == signal.side
                and v["end_ms"] <= cutoff_ms
            ][-1000:]
            X_train = [v["x"] for v in eligible]
            y_train = [v["y"] for v in eligible]
            ret_train = [v["ret"] for v in eligible]
            intervals = [(int(v["bar_ms"]), int(v["end_ms"])) for v in eligible]

        lx, ly, lr, li = self._load_ledger_closed_samples(signal.strategy_id, signal.side)
        if lx:
            X_train = (X_train + lx)[-1000:]
            y_train = (y_train + ly)[-1000:]
            ret_train = (ret_train + lr)[-1000:]
            intervals = (intervals + li)[-1000:]

        signal.features["ml_live_ledger_samples"] = len(lx)
        x_new = self._feature_vector(ctx.candles_15m)
        signal.features["ml_feature_version"] = 3
        signal.features["ml_training_samples"] = len(X_train)

        di_value = self._compute_dissimilarity_index(X_train, x_new)
        is_post_only = bool(signal.features.get("post_only", 0.0)) or signal.order_type == "Limit"
        entry_fee_pct = (self.maker_fee if is_post_only else self.taker_fee) * 100.0
        exit_fee_pct = self.taker_fee * 100.0
        slip_pct = 0.02 if is_post_only else 0.06
        total_roundtrip_cost_pct = entry_fee_pct + exit_fee_pct + ctx.screener.spread_pct + slip_pct
        cost_threshold_pct = round(self.cost_lambda * total_roundtrip_cost_pct, 4)

        target_tp1_pct = (
            abs(signal.take_profit_1 - signal.entry_price) / max(signal.entry_price, 1e-9) * 100.0
        )
        stop_pct = max(signal.stop_distance_pct, 0.01)

        # Порог P_cal по контуру: 0.52 для Разгона, 0.60 для Автотрейдинга
        if signal.strategy_id in ("S1_TREND_RETEST", "B1_SQUEEZE_BREAKOUT"):
            req_prob = 0.52
        elif signal.strategy_id in ("S3_VWAP_RANGE", "AUTOTRADE_MOMENTUM_FUNDING"):
            req_prob = 0.60
        else:
            req_prob = self.min_win_prob

        # Холодный старт (< 100 реальных сэмплов): честный статус WARMING_UP без фейковой вероятности
        if len(X_train) < self.min_samples:
            emp_wr = round(sum(y_train) / len(y_train), 4) if y_train else 0.50
            exp_ret = round(
                emp_wr * target_tp1_pct - (1.0 - emp_wr) * stop_pct - total_roundtrip_cost_pct, 3
            )
            is_wfa_ok = (
                wfa_validated
                if wfa_validated is not None
                else (signal.strategy_id in self.WFA_APPROVED_STRATEGIES)
            )
            signal.features["ml_calibration_status"] = "WARMING_UP"
            signal.ml_win_probability = emp_wr
            signal.expected_return_pct = exp_ret
            signal.cost_threshold_pct = cost_threshold_pct
            signal.dissimilarity_index = di_value
            signal.passed_ml_filter = bool(is_wfa_ok and di_value <= self.di_threshold and target_tp1_pct >= cost_threshold_pct)
            return signal

        p_cal, backend = self.fit_predict_calibrated_prob(X_train, y_train, intervals, x_new)
        expected_return_pct = round(
            p_cal * target_tp1_pct - (1.0 - p_cal) * stop_pct - total_roundtrip_cost_pct,
            3,
        )

        passed_cost = expected_return_pct > 0.0 and (p_cal * target_tp1_pct) >= cost_threshold_pct
        passed_di = di_value <= self.di_threshold
        passed_prob = p_cal >= req_prob

        signal.features["ml_calibration_status"] = backend
        signal.ml_win_probability = p_cal
        signal.expected_return_pct = expected_return_pct
        signal.cost_threshold_pct = cost_threshold_pct
        signal.dissimilarity_index = di_value
        signal.passed_ml_filter = bool(passed_cost and passed_di and passed_prob)
        return signal

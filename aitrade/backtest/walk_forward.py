"""
Честный побарный исторический Replay для стратегий S1 и S3, использующий единый модуль
aitrade.strategies.regime_s1_s3 без заглядывания в незакрытую свечу:
- Решения принимаются строго на закрытых барах candles[:i].
- S1: проверка стопа и тейка начинается с самой свечи входа fill_idx (R03).
- Учёт комиссий Bybit V5 (Maker 0.02% / Taker 0.055%) и Taker на Full TP/SL (R04).
- Хронологическая сортировка сделок по timestamp перед расчётом equity и Max DD (R07).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from aitrade.config import load_settings
from aitrade.exchange.bybit_v5 import BybitV5Client, Candle
from aitrade.features.indicators import calc_atr_series
from aitrade.strategies.regime_s1_s3 import (
    MIN_STOP_FRAC,
    generate_s1_s3_signal,
)


@dataclass
class BacktestSummary:
    mode_name: str
    total_trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float
    net_return_pct: float
    avg_r_multiple: float
    max_drawdown_pct: float
    sharpe_ratio: float
    deflated_sharpe_ratio: float


class WalkForwardBacktester:
    """Честный исторический Replay по закрытым свечам с учётом комиссий Bybit V5."""

    MIN_STOP_FRAC = MIN_STOP_FRAC

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or load_settings()
        self.client = BybitV5Client(self.settings, allow_offline_fallback=False)
        ex_cfg = self.settings.get("exchange", {})
        self.maker_fee = float(ex_cfg.get("maker_fee_rate", 0.0002))
        self.taker_fee = float(ex_cfg.get("taker_fee_rate", 0.00055))

    def _resolve_trade(
        self,
        future_bars: List[Candle],
        side: str,
        entry_px: float,
        stop_loss: float,
        take_profit: float,
        entry_fee_frac: float,
        tp_fee_frac: float,
        sl_fee_frac: float,
        move_be_at_1r: bool,
    ) -> Tuple[float, float, int]:
        risk_dist = abs(entry_px - stop_loss)
        if risk_dist <= 0 or not future_bars:
            return 0.0, 0.0, 1

        cur_sl = stop_loss
        be_armed = False
        be_px = entry_px * (1.0 + 0.0012) if side == "Buy" else entry_px * (1.0 - 0.0012)

        for idx, bar in enumerate(future_bars):
            if side == "Buy":
                hit_sl = bar.low <= cur_sl
                hit_tp = bar.high >= take_profit
                if hit_sl:
                    raw = (min(cur_sl,bar.open) - entry_px) / entry_px
                    net_pct = (raw - entry_fee_frac - sl_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if hit_tp:
                    raw = (take_profit - entry_px) / entry_px
                    net_pct = (raw - entry_fee_frac - tp_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if move_be_at_1r and not be_armed and bar.high >= entry_px + risk_dist:
                    cur_sl = max(cur_sl, be_px)
                    be_armed = True
                if move_be_at_1r and bar.high>=entry_px+1.5*risk_dist:
                    cur_sl=max(cur_sl,bar.close*(1-.0085))
            else:
                hit_sl = bar.high >= cur_sl
                hit_tp = bar.low <= take_profit
                if hit_sl:
                    raw = (entry_px - max(cur_sl,bar.open)) / entry_px
                    net_pct = (raw - entry_fee_frac - sl_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if hit_tp:
                    raw = (entry_px - take_profit) / entry_px
                    net_pct = (raw - entry_fee_frac - tp_fee_frac) * 100.0
                    return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), idx + 1
                if move_be_at_1r and not be_armed and bar.low <= entry_px - risk_dist:
                    cur_sl = min(cur_sl, be_px)
                    be_armed = True
                if move_be_at_1r and bar.low<=entry_px-1.5*risk_dist:
                    cur_sl=min(cur_sl,bar.close*(1+.0085))

        last_close = future_bars[-1].close
        raw = ((last_close - entry_px) / entry_px) if side == "Buy" else ((entry_px - last_close) / entry_px)
        net_pct = (raw - entry_fee_frac - sl_fee_frac) * 100.0
        return net_pct, net_pct / ((risk_dist / entry_px) * 100.0), len(future_bars)

    def _simulate_naive(self, candles: List[Candle]) -> List[Tuple[int, float, float, str]]:
        trades: List[Tuple[int, float, float, str]] = []
        if len(candles) < 60:
            return trades
        atr_s = calc_atr_series(candles, 14)
        taker_slip = 0.0003
        i = 24
        while i < len(candles) - 10:
            c_sig = candles[i - 1]
            c_prev = candles[i - 2]
            entry_px = candles[i].open
            atr = max(atr_s[i - 1], entry_px * 0.0015)
            side: Optional[str] = None
            if i % 3 == 0:
                side = "Buy" if c_sig.close < c_prev.close else "Sell"
            if side is None:
                i += 1
                continue
            stop_dist = max(0.75 * atr, entry_px * 0.0025)
            target_dist = 1.25 * atr
            sl = entry_px - stop_dist if side == "Buy" else entry_px + stop_dist
            tp = entry_px + target_dist if side == "Buy" else entry_px - target_dist
            net_pct, r_mult, used = self._resolve_trade(
                candles[i : i + 8],
                side,
                entry_px,
                sl,
                tp,
                self.taker_fee + taker_slip,
                self.taker_fee + taker_slip,
                self.taker_fee + taker_slip,
                move_be_at_1r=False,
            )
            trades.append((int(candles[i].timestamp), net_pct, r_mult, "NAIVE",int(candles[min(i+used-1,len(candles)-1)].timestamp)+900000,"",stop_dist/entry_px))
            i += max(2, used)
        return trades

    def replay_signal(self,candles,i,sig):
        """Shared realized label/replay; limit fill-bar profit is not assumed before fill."""
        hold=int(sig.features.get('max_hold_bars',16));fill=i
        s1=sig.strategy_id=='S1_TREND_RETEST'
        if s1:
            fill=None
            for k in range(i,min(i+int(sig.features.get('retest_ttl_bars',3)),len(candles)-hold+1)):
                if candles[k].low<=sig.entry_price<=candles[k].high:fill=k;break
            if fill is None:return None
        if fill+hold>len(candles):return None
        bars=list(candles[fill:fill+hold])
        if s1:
            # Unknown OHLC path: keep adverse excursion but discard favourable pre-fill extremes.
            from dataclasses import replace
            first=bars[0]
            bars[0]=replace(first,open=sig.entry_price,high=max(sig.entry_price,first.close) if sig.side=='Buy' else first.high,
                           low=min(sig.entry_price,first.close) if sig.side=='Sell' else first.low)
        cost=self.taker_fee+.0003  # GTC is not a maker guarantee; Full TP/SL is market execution.
        net,r,used=self._resolve_trade(bars,sig.side,sig.entry_price,sig.stop_loss,sig.take_profit_2,cost,cost,cost,s1)
        return (int(candles[fill].timestamp),net,r,sig.strategy_id,int(candles[fill+used-1].timestamp)+900000,'',abs(sig.entry_price-sig.stop_loss)/sig.entry_price)

    def _simulate_s1_s3_replay(self,candles):
        trades=[];i=220
        while i<len(candles)-26:
            sig=generate_s1_s3_signal('REPLAY',candles[:i],reference_open_px=candles[i].open,min_rr_s3=.50)
            trade=self.replay_signal(candles,i,sig) if sig else None
            if trade:
                trades.append(trade)
                while i<len(candles) and candles[i].timestamp<trade[4]:i+=1
            else:i+=1
        return trades

    def _portfolio(self,trades):
        """Event-time cash accounting; reserve risk/notional until actual exit timestamp."""
        equity=1000.;peak=equity;dd=0.;active=[];realized=[];skipped=0;self._portfolio_r=[]
        def settle(until):
            nonlocal equity,peak,dd,active
            due=sorted([x for x in active if x[0]<=until],key=lambda x:x[0])
            for end,pnl,risk,notional,symbol in due:
                equity+=pnl;realized.append(pnl);self._portfolio_r.append(pnl/risk if risk else 0.);peak=max(peak,equity);dd=max(dd,(peak-equity)/peak*100)
            active=[x for x in active if x[0]>until]
        for index,t in enumerate(sorted(trades,key=lambda t:(t[0],t[5] if len(t)>5 else ''))):
            settle(t[0]);symbol=t[5] if len(t)>5 and t[5] else str(index)
            stop=t[6] if len(t)>6 else .0055
            budget=min(1000.,max(0,equity))*.005
            notional=budget/(stop+.0054)
            if len(active)>=3 or any(x[4]==symbol for x in active) or sum(x[2] for x in active)+budget>min(equity,1000)*.02 or sum(x[3] for x in active)+notional>min(equity,1000)*3:
                skipped+=1;continue
            exit_ts=t[4] if len(t)>4 else t[0]+900000
            pnl=notional*t[1]/100
            active.append((exit_ts,pnl,budget,notional,symbol))
        settle(float('inf'))
        return realized,equity,dd,skipped

    def _summarize(self, mode_name: str, trades: List[Tuple[int, float, float, str]]) -> BacktestSummary:
        if not trades:
            return BacktestSummary(mode_name, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        pnls,equity,max_dd,skipped=self._portfolio(trades)
        if not pnls:return BacktestSummary(mode_name,0,0,0,0.,0.,0.,0.,0.,None,None)
        r_mults=list(self._portfolio_r)
        wins=[p for p in pnls if p>0];losses=[p for p in pnls if p<=0]
        win_rate=len(wins)/len(pnls)*100
        profit_factor=sum(wins)/abs(sum(losses)) if sum(losses)<0 else None
        net_ret=(equity/1000-1)*100
        mean_r=sum(r_mults)/len(r_mults)
        sharpe=None;dsr=None

        return BacktestSummary(
            mode_name=mode_name,
            total_trades=len(pnls),
            wins=len(wins),
            losses=len(losses),
            win_rate_pct=round(win_rate, 2),
            profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
            net_return_pct=round(net_ret, 2),
            avg_r_multiple=round(mean_r, 3),
            max_drawdown_pct=round(max_dd, 2),
            sharpe_ratio=None,
            deflated_sharpe_ratio=None,
        )

    def run_comparison_backtest(
        self, symbols: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        pairs = symbols or self.settings["universe"]["core_pairs"][:8]
        naive_trades: List[Tuple[int, float, float, str]] = []
        sniper_trades: List[Tuple[int, float, float, str]] = []

        provenance=[]
        for sym in pairs:
            klines = self.client.get_klines(sym, interval="15", limit=500)
            closed_klines = klines[:-1] if len(klines) > 1 else klines
            import hashlib,json
            from aitrade.config import BASE_DIR
            from aitrade.storage import atomic_json
            raw=[asdict(c) for c in closed_klines];digest=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()
            archive=BASE_DIR/'data'/'research_bars'/f'{sym}-{digest[:16]}.json'
            if not archive.exists():atomic_json(archive,raw)
            provenance.append({'symbol':sym,'sha256':digest,'bars':len(raw),'source':self.client.last_data_source,'first_ms':closed_klines[0].timestamp,'last_ms':closed_klines[-1].timestamp})
            naive_trades.extend(tuple(list(t[:5])+[sym,t[6]]) for t in self._simulate_naive(closed_klines))
            sniper_trades.extend(tuple(list(t[:5])+[sym,t[6]]) for t in self._simulate_s1_s3_replay(closed_klines))

        naive_summary = self._summarize(
            "Наивный режим (Taker без фильтра режима и 0.55% стопа)", naive_trades
        )
        sniper_summary = self._summarize(
            "Исследовательский Replay S1 + S3; консервативная модель OHLC",
            sniper_trades,
        )
        s1_trades = [t for t in sniper_trades if t[3] == "S1_TREND_RETEST"]
        s3_trades = [t for t in sniper_trades if t[3] == "S3_VWAP_RANGE"]

        return {
            "validation_status": "research_only_not_live_validated",
            "limitations": ["Нет исторического L2, очереди лимитов и полной модели funding", "News/ML/correlation gates не воспроизведены; это replay стратегий", "Просадка по реализованному PnL, не mark-to-market", "Нет независимого holdout и подтверждения прибыльности; Sharpe/DSR не рассчитаны"],
            "provenance":provenance,
            "portfolio_skipped":self._portfolio(sniper_trades)[3],
            "data_source": self.client.last_data_source,
            "pairs_tested": pairs,
            "naive_baseline": asdict(naive_summary),
            "aitrade_sniper_combine": asdict(sniper_summary),
            "strategy_breakdown": {
                "S1_TREND_RETEST": asdict(self._summarize("S1: Пробой с ретестом (1h Trend)", s1_trades)),
                "S3_VWAP_RANGE": asdict(self._summarize("S3: Возврат к VWAP ±2σ (1h Range)", s3_trades)),
            },
        }

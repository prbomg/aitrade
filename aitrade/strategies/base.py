"""
Базовые структуры данных торгового сигнала (TradeSignal) и контекста анализа актива (MarketContext).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from aitrade.exchange.bybit_v5 import Candle, OrderBookSnapshot
from aitrade.features.indicators import VolumeProfileZones, VWAPBands
from aitrade.features.microstructure import MicrostructureMetrics
from aitrade.screener.coins_in_play import ScreenerResult


@dataclass
class MarketContext:
    symbol: str
    ticker: Dict[str, Any]
    candles_5m: List[Candle]
    candles_15m: List[Candle]
    candles_1h: List[Candle]
    candles_4h: List[Candle]
    btc_candles_15m: List[Candle]
    orderbook: OrderBookSnapshot
    oi_history: List[Tuple[int, float]]
    vwap_bands: VWAPBands
    volume_profile: VolumeProfileZones
    micro: MicrostructureMetrics
    screener: ScreenerResult


@dataclass
class TradeSignal:
    symbol: str
    strategy_id: str
    strategy_name: str
    timeframe: str
    side: str  # "Buy" (Long) | "Sell" (Short)
    order_type: str  # "Limit" (Maker PostOnly) | "Market"
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    base_winrate_est: float  # Исторический базовый винрейт сетапа (0.0..1.0)
    rationale: List[str] = field(default_factory=list)
    invalidation_rule: str = "Закрытие за уровнем SL или отсутствие импульса за 2 бара (2-Bar Stall Rule)"
    features: Dict[str, float] = field(default_factory=dict)

    # Поля, заполняемые на уровне ML-валидатора, Дебатов агентов и RiskEngine:
    ml_win_probability: float = 0.0
    expected_return_pct: float = 0.0
    cost_threshold_pct: float = 0.0
    dissimilarity_index: float = 0.0
    passed_ml_filter: bool = False
    debate_verdict: str = "PENDING"
    debate_regime: str = "NEUTRAL"
    debate_summary: str = ""
    position_size_usd: float = 0.0
    position_qty: float = 0.0
    effective_leverage: float = 1.0
    liquidation_price: float = 0.0
    stop_to_liq_multiple: float = 0.0
    estimated_l2_slippage_bps: float = 0.0
    risk_amount_usd: float = 0.0
    signal_id: str = ""
    signal_created_ms: int = 0

    @property
    def stop_distance_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return abs(self.entry_price - self.stop_loss) / self.entry_price * 100.0

    @property
    def reward_to_risk_tp1(self) -> float:
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit_1 - self.entry_price)
        return reward / max(risk, 1e-9)

    @property
    def reward_to_risk_tp2(self) -> float:
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit_2 - self.entry_price)
        return reward / max(risk, 1e-9)

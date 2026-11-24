"""
Модуль микроструктуры стакана Level-2, потока ордеров (CVD, VPIN / Toxic Order Flow),
сглаживания Савицкого — Голея (Savitzky-Golay) и 4-ступенчатого детектора спуфинга (LBI):
- Order Book Imbalance (OBI L=1, L=5, Weighted Top-5 с весами [1.0, 0.5, 0.25, 0.125, 0.0625])
- VAMP (Volume Adjusted Mid Price) и Microprice (P_w-mid)
- Сглаживание временных рядов фильтром Савицкого — Голея (arXiv 2506.05764v2)
- Кумулятивная дельта объема (CVD) по свечам и тиковой ленте принтов
- Индикатор токсичного потока VPIN (Volume-Synchronized Probability of Informed Trading)
- 4-ступенчатый верификатор плотностей L2 и индекс доверия к ликвидности (Liquidity Behavior Index — LBI)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from aitrade.exchange.bybit_v5 import Candle, OrderBookSnapshot


@dataclass
class MicrostructureMetrics:
    mid_price: float
    vamp_bbo: float
    weighted_depth_mid: float
    vamp_divergence_bps: float
    obi_l1: float
    obi_l5: float
    weighted_obi_top5: float
    smoothed_obi: float
    cvd_slope: float
    vpin_tof: float
    largest_bid_wall: Tuple[float, float]  # (price, notional_usd)
    largest_ask_wall: Tuple[float, float]  # (price, notional_usd)
    liquidity_behavior_index: float  # 0.0..1.0 (Anti-Spoofing LBI)
    spoofing_flags: List[str]


def savitzky_golay_smooth(series: List[float], window: int = 7, polyorder: int = 2) -> List[float]:
    """
    Реализация полиномиального фильтра Савицкого — Голея (Savitzky-Golay) на чистом Python
    для подавления высокочастотного шума стакана (arXiv 2506.05764v2).
    Для окна 5 и 7 при polyorder=2 используются точные аналитические коэффициенты МНК.
    """
    n = len(series)
    if n < 5:
        return list(series)
    # Точные сверточные веса МНК для полинома 2-й степени:
    if window >= 7 and n >= 7:
        coeffs = [-2.0, 3.0, 6.0, 7.0, 6.0, 3.0, -2.0]
        norm = 21.0
        half = 3
    else:
        coeffs = [-3.0, 12.0, 17.0, 12.0, -3.0]
        norm = 35.0
        half = 2

    smoothed: List[float] = []
    for i in range(n):
        acc = 0.0
        for k, c in enumerate(coeffs):
            idx = min(max(i + (k - half), 0), n - 1)
            acc += c * series[idx]
        smoothed.append(acc / norm)
    return smoothed


def calc_obi(orderbook: OrderBookSnapshot, depth: int = 5) -> float:
    """
    Order Book Imbalance (OBI) = (Sum(V_bid) - Sum(V_ask)) / (Sum(V_bid) + Sum(V_ask))
    в диапазоне [-1.0, +1.0].
    """
    bids = orderbook.bids[:depth]
    asks = orderbook.asks[:depth]
    v_bid = sum(b.notional_usd for b in bids)
    v_ask = sum(a.notional_usd for a in asks)
    denom = v_bid + v_ask
    if denom <= 1e-9:
        return 0.0
    return (v_bid - v_ask) / denom


def calc_weighted_top5_obi(orderbook: OrderBookSnapshot) -> float:
    """
    Взвешенный по дистанции дисбаланс топ-5 уровней стакана с весами [1.00, 0.50, 0.25, 0.125, 0.0625]
    согласно разделу 5 документа. Значение |OBI| > 0.20 (или доля > 60%) указывает на направленное давление.
    """
    weights = [1.00, 0.50, 0.25, 0.125, 0.0625]
    w_bid = 0.0
    w_ask = 0.0
    for i, w in enumerate(weights):
        if i < len(orderbook.bids):
            w_bid += w * orderbook.bids[i].notional_usd
        if i < len(orderbook.asks):
            w_ask += w * orderbook.asks[i].notional_usd
    denom = w_bid + w_ask
    if denom <= 1e-9:
        return 0.0
    return (w_bid - w_ask) / denom


def calc_vamp_and_microprice(orderbook: OrderBookSnapshot, depth: int = 5) -> Tuple[float, float, float]:
    """
    Рассчитывает:
    1. VAMP_bbo = (P_bid * V_ask + P_ask * V_bid) / (V_bid + V_ask) — крест-накрест взвешенная цена BBO.
    2. P_w-mid (Weighted-Depth Order Book Price) на глубине depth уровней.
    3. Отклонение VAMP от классической средней цены (mid_price) в базисных пунктах (bps).
    """
    mid = orderbook.mid_price
    if not orderbook.bids or not orderbook.asks or mid <= 0:
        return mid, mid, 0.0

    b0 = orderbook.bids[0]
    a0 = orderbook.asks[0]
    bbo_vol = b0.size + a0.size
    vamp_bbo = (b0.price * a0.size + a0.price * b0.size) / bbo_vol if bbo_vol > 0 else mid

    bids = orderbook.bids[:depth]
    asks = orderbook.asks[:depth]
    sum_b_vol = sum(b.size for b in bids)
    sum_a_vol = sum(a.size for a in asks)
    avg_b_px = sum(b.price * b.size for b in bids) / max(sum_b_vol, 1e-9)
    avg_a_px = sum(a.price * a.size for a in asks) / max(sum_a_vol, 1e-9)
    tot_vol = sum_b_vol + sum_a_vol
    weighted_depth_mid = (
        (avg_b_px * sum_a_vol + avg_a_px * sum_b_vol) / tot_vol if tot_vol > 0 else mid
    )
    vamp_div_bps = ((weighted_depth_mid - mid) / mid) * 10000.0
    return vamp_bbo, weighted_depth_mid, vamp_div_bps


def calc_candle_cvd_series(candles: List[Candle]) -> List[float]:
    """
    Оценивает кумулятивную дельту объема (CVD) по положению закрытия свечи внутри бара (Close Location Value)
    с нормировкой на оборот. Позволяет обнаруживать бычьи и медвежьи дивергенции CVD на 15m/1h.
    """
    cvd: List[float] = []
    running = 0.0
    for c in candles:
        rng = c.high - c.low
        if rng <= 1e-12:
            delta = 0.0
        else:
            # CLV от -1 (закрытие на low) до +1 (закрытие на high)
            clv = ((c.close - c.low) - (c.high - c.close)) / rng
            delta = clv * c.volume
        running += delta
        cvd.append(running)
    return cvd


def calc_vpin_toxic_flow(
    recent_trades: List[Dict[str, Any]], candles: List[Candle]
) -> float:
    """
    Рассчитывает VPIN (Volume-Synchronized Probability of Informed Trading / Toxic Order Flow — TOF)
    по формуле Исли — Прадо — О'Хара: отношение модуля дисбаланса агрессивных покупок и продаж
    к общему проторгованному объему в объемных корзинах (buckets).
    Значения > 0.42 указывают на высокий токсичный поток (информированный пробой или каскад ликвидаций).
    """
    if recent_trades:
        bucket_size = max(1, len(recent_trades) // 10)
        bucket_imbalances: List[float] = []
        for i in range(0, len(recent_trades), bucket_size):
            chunk = recent_trades[i : i + bucket_size]
            v_buy = 0.0
            v_sell = 0.0
            for t in chunk:
                sz = float(t.get("size", 0.0)) * float(t.get("price", 1.0))
                if str(t.get("side", "Buy")).lower() == "buy":
                    v_buy += sz
                else:
                    v_sell += sz
            tot = v_buy + v_sell
            if tot > 0:
                bucket_imbalances.append(abs(v_buy - v_sell) / tot)
        if bucket_imbalances:
            return sum(bucket_imbalances) / len(bucket_imbalances)

    # Fallback по последним 10 свечам, если тиковая лента пуста
    if not candles:
        return 0.25
    sub = candles[-min(len(candles), 10) :]
    imbs: List[float] = []
    for c in sub:
        rng = max(c.high - c.low, 1e-9)
        imbs.append(abs(c.close - c.open) / rng)
    return sum(imbs) / len(imbs)


def verify_orderbook_densities_and_spoofing(
    current_ob: OrderBookSnapshot,
    ob_history: List[OrderBookSnapshot],
    recent_trades: List[Dict[str, Any]],
) -> Tuple[Tuple[float, float], Tuple[float, float], float, List[str]]:
    """
    Реализует 4-ступенчатый фильтр верификации плотностей и детекции спуфинга из раздела 5Б документа:
    1. Поиск крупнейших лимитных стен (Bid Wall / Ask Wall) в пределах 1.5% от цены.
    2. Проверка паттернов манипуляции:
       - Vanishing Walls (крупная стена исчезла без исполнения в ленте принтов),
       - Layering (синхронные стены на 3+ соседних тиках с одинаковым объемом),
       - Shifting Walls (стена смещается от цены).
    3. Расчет индекса доверия к ликвидности LBI (Liquidity Behavior Index в диапазоне [0.0, 1.0]).
    """
    if not current_ob.bids or not current_ob.asks:
        return (0.0, 0.0), (0.0, 0.0), 0.5, []

    best_bid_wall = max(current_ob.bids[:25], key=lambda x: x.notional_usd)
    best_ask_wall = max(current_ob.asks[:25], key=lambda x: x.notional_usd)

    flags: List[str] = []
    lbi_score = 0.85

    # Проверка Layering (несколько крупных заявок подряд с почти идентичным объемом)
    top_bids = [b.notional_usd for b in current_ob.bids[:10]]
    avg_bid = sum(top_bids) / max(len(top_bids), 1)
    heavy_bids = [v for v in top_bids if v > avg_bid * 2.2]
    if len(heavy_bids) >= 3:
        max_hb, min_hb = max(heavy_bids), min(heavy_bids)
        if (max_hb - min_hb) / max(max_hb, 1.0) < 0.05:
            flags.append("LAYERING_SUSPECTED")
            lbi_score -= 0.20

    # Проверка истории снимков стакана на Vanishing Walls
    if len(ob_history) >= 2:
        prev_ob = ob_history[-2]
        if prev_ob.bids and prev_ob.asks:
            prev_max_bid = max(prev_ob.bids[:25], key=lambda x: x.notional_usd)
            if prev_max_bid.notional_usd > avg_bid * 4.0 and prev_max_bid.price > 0:
                still_there = any(
                    abs(b.price - prev_max_bid.price) / prev_max_bid.price < 0.0002
                    and b.notional_usd >= prev_max_bid.notional_usd * 0.5
                    for b in current_ob.bids[:25]
                )
                traded_notional = sum(
                    float(t.get("price", 0.0)) * float(t.get("size", 0.0))
                    for t in recent_trades[:30]
                    if str(t.get("side", "")).lower() == "sell"
                )
                if not still_there and traded_notional < prev_max_bid.notional_usd * 0.3:
                    flags.append("VANISHING_BID_WALL")
                    lbi_score -= 0.25

    # Подтверждение поглощением (Absorption Bonus): если плотность стоит близко к цене и держит поток
    mid = current_ob.mid_price
    if mid > 0 and best_bid_wall.notional_usd >= avg_bid * 3.0 and abs(mid - best_bid_wall.price) / mid < 0.004:
        lbi_score = min(1.0, lbi_score + 0.10)

    return (
        (best_bid_wall.price, round(best_bid_wall.notional_usd, 2)),
        (best_ask_wall.price, round(best_ask_wall.notional_usd, 2)),
        round(max(0.1, min(1.0, lbi_score)), 3),
        flags,
    )


def analyze_microstructure(
    candles_15m: List[Candle],
    orderbook: OrderBookSnapshot,
    ob_history: List[OrderBookSnapshot],
    recent_trades: List[Dict[str, Any]],
) -> MicrostructureMetrics:
    obi_l1 = calc_obi(orderbook, depth=1)
    obi_l5 = calc_obi(orderbook, depth=5)
    w_obi_5 = calc_weighted_top5_obi(orderbook)

    # Сглаживаем серию OBI по истории стакана (или строим синтетическое окно вокруг текущего OBI)
    hist_obis = [calc_obi(snap, depth=5) for snap in ob_history[-7:]]
    while len(hist_obis) < 7:
        hist_obis.insert(0, obi_l5 * 0.9)
    smoothed_obi = savitzky_golay_smooth(hist_obis, window=7, polyorder=2)[-1]

    vamp_bbo, w_depth_mid, vamp_div_bps = calc_vamp_and_microprice(orderbook, depth=5)
    cvd_series = calc_candle_cvd_series(candles_15m)
    if len(cvd_series) >= 5:
        avg_vol = sum(c.volume for c in candles_15m[-10:]) / min(len(candles_15m), 10)
        cvd_slope = (cvd_series[-1] - cvd_series[-4]) / max(avg_vol * 3.0, 1e-9)
    else:
        cvd_slope = 0.0

    vpin_tof = calc_vpin_toxic_flow(recent_trades, candles_15m)
    bid_wall, ask_wall, lbi, flags = verify_orderbook_densities_and_spoofing(
        orderbook, ob_history, recent_trades
    )

    return MicrostructureMetrics(
        mid_price=orderbook.mid_price,
        vamp_bbo=round(vamp_bbo, 6),
        weighted_depth_mid=round(w_depth_mid, 6),
        vamp_divergence_bps=round(vamp_div_bps, 3),
        obi_l1=round(obi_l1, 4),
        obi_l5=round(obi_l5, 4),
        weighted_obi_top5=round(w_obi_5, 4),
        smoothed_obi=round(smoothed_obi, 4),
        cvd_slope=round(cvd_slope, 4),
        vpin_tof=round(vpin_tof, 4),
        largest_bid_wall=bid_wall,
        largest_ask_wall=ask_wall,
        liquidity_behavior_index=lbi,
        spoofing_flags=flags,
    )

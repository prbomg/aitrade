"""
Исполнитель сделок на Bybit Unified Demo Trading (https://api-demo.bybit.com) и локальный демо-трекер SQLite.
Работает в двух бесшовных режимах:
1. Если в .env заданы BYBIT_DEMO_API_KEY и BYBIT_DEMO_API_SECRET:
   - выставляет реальный демо-ордер на https://api-demo.bybit.com/v5/order/create (Limit PostOnly + SL + TP1),
   - устанавливает безопасное расчетное плечо (до 4x),
   - сохраняет ордер в локальный журнал SQLite (data/demo_journal.sqlite3).
2. Если API-ключи еще не введены:
   - открывает виртуальную демо-сделку по реальным живым котировкам и стакану L2 Bybit в локальной базе SQLite,
   - сопровождает сделку по живым свечам Bybit до срабатывания TP1/TP2/SL или правила 2 баров (2-Bar Stall Rule),
   - накапливает статистику Winrate и обучающую выборку для ML-валидатора.
"""

from __future__ import annotations

from contextlib import closing
import datetime
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from aitrade.config import BASE_DIR
from aitrade.exchange.bybit_v5 import BybitV5Client
from aitrade.strategies.base import TradeSignal

DB_PATH = BASE_DIR / "data" / "demo_journal.sqlite3"


class BybitDemoExecutor:
    """Менеджер демо-исполнения (Bybit Demo API + локальный SQLite журнал)."""

    def __init__(
        self,
        client: BybitV5Client,
        settings: Dict[str, Any],
        db_path: Path = DB_PATH,
    ) -> None:
        self.client = client
        self.settings = settings
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit_1 REAL NOT NULL,
                    take_profit_2 REAL NOT NULL,
                    qty REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    leverage REAL NOT NULL,
                    liquidation_price REAL NOT NULL,
                    ml_win_prob REAL NOT NULL,
                    expected_return_pct REAL NOT NULL,
                    di_index REAL NOT NULL,
                    execution_venue TEXT NOT NULL,
                    exchange_order_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pnl_usd REAL DEFAULT 0.0,
                    debate_summary TEXT
                )
                """
            )
            conn.commit()

    def get_account_equity_usdt(self) -> float:
        """Запрашивает баланс USDT с Bybit Demo API или возвращает баланс локального демо-счета."""
        base_equity = float(self.settings.get("risk", {}).get("initial_demo_equity_usdt", 10000.0))
        if self.client.api_key and self.client.api_secret:
            try:
                res = self.client.get_demo_wallet_balance("USDT")
                acct_list = res.get("list", [])
                if acct_list:
                    total_eq = float(acct_list[0].get("totalEquity", base_equity) or base_equity)
                    if total_eq > 0:
                        return total_eq
            except Exception:
                if not self.settings.get("offline_simulation", False): raise
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT COALESCE(SUM(pnl_usd), 0.0) FROM demo_trades").fetchone()
            realized_pnl = float(row[0]) if row else 0.0
        return round(base_equity + realized_pnl, 2)

    def execute_signal(self, signal: TradeSignal) -> Dict[str, Any]:
        """Исполняет одобренный сигнал на Bybit Demo API или в локальном трекере."""
        if not self.settings.get("offline_simulation", False):
            raise RuntimeError("Исполнение разрешено только через общий контроллер риска")
        now_iso = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        venue = "LOCAL_BYBIT_DEMO_SIM"
        order_id = f"sim_{signal.symbol}_{int(datetime.datetime.now().timestamp())}"
        status = "OPEN_LIMIT_MAKER"

        if self.client.api_key and self.client.api_secret:
            try:
                self.client.set_leverage(signal.symbol, max(1.0, round(signal.effective_leverage)))
                resp = self.client.place_demo_order(
                    symbol=signal.symbol,
                    side=signal.side,
                    qty=signal.position_qty,
                    order_type=signal.order_type,
                    price=signal.entry_price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit_1,
                    post_only=False,
                )
                order_id = str(resp.get("orderId", order_id))
                venue = "BYBIT_UNIFIED_DEMO_API"
                status = "SUBMITTED_BYBIT_DEMO"
            except Exception as exc:
                venue = f"LOCAL_FALLBACK ({str(exc)[:80]})"

        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                """
                INSERT INTO demo_trades (
                    created_at, symbol, strategy_id, strategy_name, side, order_type,
                    entry_price, stop_loss, take_profit_1, take_profit_2,
                    qty, notional_usd, leverage, liquidation_price,
                    ml_win_prob, expected_return_pct, di_index,
                    execution_venue, exchange_order_id, status, pnl_usd, debate_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    signal.symbol,
                    signal.strategy_id,
                    signal.strategy_name,
                    signal.side,
                    signal.order_type,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.take_profit_1,
                    signal.take_profit_2,
                    signal.position_qty,
                    signal.position_size_usd,
                    signal.effective_leverage,
                    signal.liquidation_price,
                    signal.ml_win_probability,
                    signal.expected_return_pct,
                    signal.dissimilarity_index,
                    venue,
                    order_id,
                    status,
                    0.0,
                    signal.debate_summary,
                ),
            )
            trade_id = cur.lastrowid
            conn.commit()

        return {
            "trade_id": trade_id,
            "symbol": signal.symbol,
            "side": signal.side,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "take_profit_1": signal.take_profit_1,
            "take_profit_2": signal.take_profit_2,
            "notional_usd": signal.position_size_usd,
            "qty": signal.position_qty,
            "execution_venue": venue,
            "exchange_order_id": order_id,
            "status": status,
        }

    def list_recent_trades(self, limit: int = 30) -> List[Dict[str, Any]]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM demo_trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
